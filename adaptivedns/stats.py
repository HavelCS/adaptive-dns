"""Live metrics streamed to the dashboard."""
from __future__ import annotations

import time
from collections import Counter, deque

OUTCOMES = ("HIT", "PREFETCHED", "NEG_HIT", "COALESCED", "MISS", "STALE", "SERVFAIL", "BLOCKED")
FROM_CACHE = {"HIT", "PREFETCHED", "NEG_HIT", "COALESCED", "STALE"}


class Stats:
    def __init__(self, history=180, log_size=400):
        self.history = history
        self.started = time.time()
        self.outcomes = Counter()
        self.qtypes = Counter()
        self.rcodes = Counter()
        self.lat = {"cache": deque(maxlen=600), "upstream": deque(maxlen=600)}
        self.upstream_queries = 0      # every packet we sent upstream (incl. prefetch)
        self.prefetches = 0
        self.upstream_failures = 0
        self.time_saved_ms = 0.0
        self.verified = 0              # demo only: answers checked against the zone's truth
        self.outdated = 0
        self.buckets = deque(maxlen=history)
        self.log = deque(maxlen=log_size)
        self.seq = 0

    def reset(self):
        self.__init__(self.history, self.log.maxlen)

    @staticmethod
    def _empty(t):
        return {"t": t, "cache": 0, "upstream": 0, "fail": 0, "up_q": 0, "prefetch": 0}

    def _bucket(self, now=None):
        sec = int(now or time.time())
        if not self.buckets or self.buckets[-1]["t"] != sec:
            if self.buckets:
                for t in range(max(self.buckets[-1]["t"] + 1, sec - self.history + 1), sec):
                    self.buckets.append(self._empty(t))
            self.buckets.append(self._empty(sec))
        return self.buckets[-1]

    def upstream_sent(self, prefetch=False):
        self.upstream_queries += 1
        b = self._bucket()
        b["up_q"] += 1
        if prefetch:
            self.prefetches += 1
            b["prefetch"] += 1

    def record(self, *, name, qtype, outcome, rcode, ms, saved_ms=0.0, answer="", client="", proto="udp",
               note="", outdated=None):
        self.seq += 1
        self.outcomes[outcome] += 1
        self.qtypes[qtype] += 1
        self.rcodes[rcode] += 1
        b = self._bucket()
        if outcome in FROM_CACHE:
            b["cache"] += 1
            if outcome in ("HIT", "PREFETCHED", "NEG_HIT"):
                self.lat["cache"].append(ms)
            self.time_saved_ms += max(0.0, saved_ms - ms)
        elif outcome == "MISS":
            b["upstream"] += 1
            self.lat["upstream"].append(ms)
        else:
            b["fail"] += 1
        if outdated is not None:
            self.verified += 1
            self.outdated += bool(outdated)
        self.log.append({"id": self.seq, "ts": time.time(), "name": name, "type": qtype, "outcome": outcome,
                         "rcode": rcode, "ms": round(ms, 3), "answer": answer, "client": client,
                         "proto": proto, "note": note, "outdated": outdated})

    @staticmethod
    def _avg(xs):
        return round(sum(xs) / len(xs), 3) if xs else None

    @staticmethod
    def _pct(xs, p):
        if not xs:
            return None
        s = sorted(xs)
        return round(s[min(len(s) - 1, int(len(s) * p))], 3)

    def summary(self):
        total = sum(self.outcomes.values())
        answered_from_cache = sum(self.outcomes[o] for o in FROM_CACHE)
        lookups = answered_from_cache + self.outcomes["MISS"] + self.outcomes["SERVFAIL"]
        return {
            "uptime": round(time.time() - self.started),
            "queries": total,
            "outcomes": {o: self.outcomes[o] for o in OUTCOMES},
            "hit_ratio": round(answered_from_cache / lookups, 4) if lookups else 0.0,
            "upstream_queries": self.upstream_queries,
            "prefetches": self.prefetches,
            "upstream_failures": self.upstream_failures,
            "upstream_saved": max(0, total - self.upstream_queries),
            "time_saved_ms": round(self.time_saved_ms, 1),
            "latency": {"cache_avg": self._avg(self.lat["cache"]), "cache_p95": self._pct(self.lat["cache"], .95),
                        "upstream_avg": self._avg(self.lat["upstream"]),
                        "upstream_p95": self._pct(self.lat["upstream"], .95)},
            "qtypes": dict(self.qtypes.most_common(6)),
            "rcodes": dict(self.rcodes),
            "verified": self.verified, "outdated": self.outdated,
        }

    def series(self, n=90):
        self._bucket()
        return list(self.buckets)[-n:]

    def recent(self, after=0, limit=60):
        return [r for r in list(self.log)[-limit:] if r["id"] > after]
