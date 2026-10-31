"""
Popularity- and Stability-Aware Adaptive DNS Cache  (the proposed algorithm)

A standard caching resolver stores each answer for exactly its TTL and
evicts with LRU. This cache adds six mechanisms, each of which can be
switched off for the ablation study:

 1. ARC replacement        Keeps names that are asked for repeatedly even
                           when a burst of one-off lookups (typos, trackers,
                           crawlers) passes through. See policies.py.

 2. Popularity-aware       Every entry carries a popularity score: an
    prefetching            exponentially decaying hit count
                               score = score * e^(-dt/tau) + 1
                           When a popular entry enters the last 15% of its
                           TTL it is refreshed in the background, so users
                           never wait for it to expire. Unpopular entries are
                           left to expire - that is what keeps prefetching
                           cheap instead of re-fetching the whole cache.

 3. Stability-learned      For each name the cache tracks how long its answer
    TTL stretching         actually stays the same (the observed lifetime: a
                           smoothed average of the time between changes, or
                           how long the current answer has lasted if it has
                           never changed). The cached lifetime may be raised
                           above the TTL, but never beyond
                               safety x observed lifetime   (safety = 0.1)
                           and at most 4x the TTL. Names whose owners set tiny
                           TTLs on records that never change get cached
                           longer; names that really do change are not
                           stretched, which keeps outdated answers rare.

 4. Negative caching       NXDOMAIN and NODATA answers are cached for the SOA
    (RFC 2308)             minimum, so repeated typos/dead trackers don't go
                           upstream each time.

 5. Serve-stale            If every upstream fails, an expired answer is
    (RFC 8767)             served (with a short TTL) instead of SERVFAIL.

 6. Query coalescing       (in resolver.py) Concurrent identical misses share
                           one upstream query.

The clock is injectable so the exact same code runs in real time (the live
resolver) and in virtual time (the evaluation in experiment.py).
"""
from __future__ import annotations

import hashlib
import math
import time
from dataclasses import dataclass, field

from . import dnsmsg as d
from .policies import ARCPolicy, LFUPolicy, make_policy

FEATURES = ("arc", "prefetch", "stretch", "negative", "serve_stale", "coalesce")


@dataclass
class Entry:
    key: tuple
    rcode: int
    answers: list
    authority: list
    stored_at: float
    orig_ttl: float
    ttl: float
    signature: str
    fetch_ms: float
    hits: int = 0
    score: float = 0.0
    last_access: float = 0.0
    refreshing: bool = False
    prefetches: int = 0
    warmed: bool = False      # last refresh was a prefetch
    stretch: float = 1.0

    @property
    def negative(self):
        return self.rcode == d.NXDOMAIN or not self.answers

    @property
    def expires_at(self):
        return self.stored_at + self.ttl

    def remaining(self, now):
        return self.expires_at - now

    def size(self):
        return 64 + sum(len(r.name) + len(r.rdata) + 12 for r in self.answers + self.authority)


@dataclass
class Learned:
    signature: str = ""
    checks: int = 0
    streak: int = 0
    changes: int = 0
    since: float = 0.0          # when the current answer was first seen
    lifetime: float = 0.0       # smoothed observed lifetime of an answer (0 = never seen a change)


def signature(rcode, answers) -> str:
    # order-insensitive: round-robin reordering is not a change
    parts = sorted(f"{r.name.lower()}|{r.rtype}|{r.rdata.hex()}" for r in answers)
    return hashlib.sha1((str(rcode) + ";" + ";".join(parts)).encode()).hexdigest()[:16]


class DNSCache:
    def __init__(self, max_entries=400, policy=None, *, arc=True, prefetch=True, stretch=True,
                 negative=True, serve_stale=True, coalesce=True,
                 max_stretch=4.0, stretch_cap=3600.0, stretch_safety=0.1, prefetch_window=0.15, prefetch_threshold=3.0,
                 popularity_tau=120.0, stale_ttl=30, stale_max_age=86400.0, min_ttl=0, clock=time.time):
        self.clock = clock
        self.max_entries = max_entries
        self.features = {"arc": arc, "prefetch": prefetch, "stretch": stretch, "negative": negative,
                         "serve_stale": serve_stale, "coalesce": coalesce}
        self.policy = make_policy(policy or ("ARC" if arc else "LRU"), max_entries)
        self.max_stretch = max_stretch
        self.stretch_cap = stretch_cap
        self.stretch_safety = stretch_safety
        self.prefetch_window = prefetch_window
        self.prefetch_threshold = prefetch_threshold
        self.tau = popularity_tau
        self.stale_ttl = stale_ttl
        self.stale_max_age = stale_max_age
        self.min_ttl = min_ttl
        self.entries: dict[tuple, Entry] = {}
        self.learned: dict[tuple, Learned] = {}
        self.evictions = 0

    # -- configuration --------------------------------------------------
    def set_feature(self, name, on: bool):
        if name not in FEATURES:
            raise ValueError(name)
        self.features[name] = bool(on)
        if name == "arc":
            self.set_policy("ARC" if on else "LRU")

    def set_policy(self, name, max_entries=None):
        if max_entries:
            self.max_entries = int(max_entries)
        old = sorted(self.entries.values(), key=lambda e: e.last_access)
        self.policy = make_policy(name, self.max_entries)
        self.features["arc"] = self.policy.name == "ARC"
        self.entries = {}
        for e in old:
            self._insert(e)

    # -- popularity -------------------------------------------------------
    def popularity(self, e: Entry, now=None) -> float:
        now = now if now is not None else self.clock()
        return e.score * math.exp(-(now - e.last_access) / self.tau)

    # -- lookup -------------------------------------------------------------
    def lookup(self, key):
        """Returns (entry, 'fresh'|'stale') or (None, None)."""
        e = self.entries.get(key)
        if e is None:
            return None, None
        now = self.clock()
        e.score = self.popularity(e, now) + 1
        e.last_access = now
        e.hits += 1
        self.policy.access(key)
        return e, ("fresh" if e.remaining(now) > 0 else "stale")

    def peek(self, key):
        return self.entries.get(key)

    def can_serve_stale(self, e: Entry) -> bool:
        return (self.features["serve_stale"] and e is not None
                and self.clock() - e.expires_at < self.stale_max_age)

    def answer_records(self, e: Entry, stale=False):
        """Records with TTLs counted down to what remains."""
        now = self.clock()
        ttl = self.stale_ttl if stale else max(1, int(e.remaining(now)))
        return [r.with_ttl(ttl) for r in e.answers], [r.with_ttl(ttl) for r in e.authority]

    # -- storing --------------------------------------------------------------
    def stretched_ttl(self, key, ttl: float) -> float:
        if not self.features["stretch"]:
            return ttl
        L = self.learned.get(key)
        if not L or L.checks == 0:
            return ttl
        # never seen a change: trust how long the current answer has lasted;
        # seen changes: use the smoothed lifetime between changes
        est = L.lifetime if L.changes else self.clock() - L.since
        safe = self.stretch_safety * est
        return max(ttl, min(safe, ttl * self.max_stretch, max(ttl, self.stretch_cap)))

    def learn(self, key, sig) -> Learned:
        now = self.clock()
        L = self.learned.setdefault(key, Learned())
        if L.signature:
            L.checks += 1
            if sig == L.signature:
                L.streak += 1
            else:
                lived = now - L.since
                L.lifetime = lived if L.changes == 0 else 0.7 * L.lifetime + 0.3 * lived
                L.streak = 0
                L.changes += 1
                L.since = now
        else:
            L.since = now
        L.signature = sig
        return L

    def store(self, key, rcode, answers, authority, fetch_ms, prefetch=False):
        if rcode not in (d.NOERROR, d.NXDOMAIN):
            return None   # SERVFAIL/REFUSED are not cached
        negative = rcode == d.NXDOMAIN or not answers
        if negative and not self.features["negative"]:
            self.remove(key)
            return None
        if negative:
            soa = next((r for r in authority if r.rtype == d.SOA), None)
            ttl = min(soa.ttl, d.soa_minimum(soa)) if soa else 0
        else:
            ttl = min(r.ttl for r in answers)
        ttl = max(ttl, self.min_ttl)
        if ttl <= 0:
            return None

        sig = signature(rcode, answers)
        self.learn(key, sig)
        eff = self.stretched_ttl(key, ttl) if not negative else ttl

        now = self.clock()
        old = self.entries.get(key)
        e = Entry(key, rcode, list(answers), list(authority), now, float(ttl), float(eff), sig, fetch_ms,
                  last_access=old.last_access if old else now, stretch=round(eff / ttl, 2))
        if old:
            e.hits, e.score, e.prefetches = old.hits, old.score, old.prefetches + (1 if prefetch else 0)
            e.warmed = prefetch
            self.entries[key] = e
            if not prefetch:   # a background refresh is not a user access
                self.policy.access(key)
        else:
            self._insert(e)
        return e

    def _insert(self, e: Entry):
        for v in self.policy.admit(e.key):
            if self.entries.pop(v, None) is not None:
                self.evictions += 1
        self.entries[e.key] = e

    def remove(self, key):
        if key in self.entries:
            self.policy.remove(key)
            del self.entries[key]
            return True
        return False

    def clear(self):
        self.entries.clear()
        self.policy = make_policy(self.policy.name, self.max_entries)

    # -- prefetching ----------------------------------------------------------
    def prefetch_candidates(self, limit=20):
        if not self.features["prefetch"]:
            return []
        now = self.clock()
        out = []
        for e in self.entries.values():
            if e.refreshing or e.negative:
                continue
            rem = e.remaining(now)
            if 0 < rem <= max(1.0, e.ttl * self.prefetch_window):
                p = self.popularity(e, now)
                if p >= self.prefetch_threshold:
                    out.append((p, e))
        out.sort(key=lambda x: -x[0])
        return [e for _, e in out[:limit]]

    def purge(self):
        """Drop entries that are too old even to be served stale."""
        now = self.clock()
        limit = self.stale_max_age if self.features["serve_stale"] else 0
        dead = [k for k, e in self.entries.items() if now - e.expires_at > limit]
        for k in dead:
            self.remove(k)
        return len(dead)

    # -- dashboard ------------------------------------------------------------
    def describe(self):
        dsc = self.policy.describe()
        dsc.update(entries=len(self.entries), max_entries=self.max_entries, evictions=self.evictions,
                   features=dict(self.features), learned=len(self.learned),
                   stretched=sum(1 for e in self.entries.values() if e.stretch > 1),
                   negative=sum(1 for e in self.entries.values() if e.negative))
        return dsc

    def snapshot(self, limit=200):
        now = self.clock()
        rows = []
        for e in sorted(self.entries.values(), key=lambda e: -self.popularity(e, now))[:limit]:
            L = self.learned.get(e.key)
            seg = self.policy.segment(e.key) if isinstance(self.policy, ARCPolicy) else None
            if isinstance(self.policy, LFUPolicy):
                seg = f"f={self.policy.frequency(e.key)}"
            rows.append({
                "name": e.key[0], "type": d.type_name(e.key[1]), "rcode": d.RCODES.get(e.rcode, e.rcode),
                "data": [r.text for r in e.answers if r.rtype == e.key[1]][:3] or
                        ([r.text for r in e.answers][-1:] if e.answers else []),
                "cname": [r.text for r in e.answers if r.rtype == d.CNAME][:1],
                "negative": e.negative, "orig_ttl": round(e.orig_ttl), "ttl": round(e.ttl),
                "remaining": round(e.remaining(now), 1), "stretch": e.stretch,
                "hits": e.hits, "popularity": round(self.popularity(e, now), 2), "segment": seg,
                "prefetches": e.prefetches, "fetch_ms": round(e.fetch_ms, 1),
                "streak": L.streak if L else 0, "changes": L.changes if L else 0,
                "lifetime": round(L.lifetime) if L and L.changes else None,
                "stable_for": round(now - L.since) if L else 0,
            })
        return rows
