"""
Evaluation: replay one identical workload through several resolver
configurations in *virtual time* and compare them.

Why virtual time? DNS behaviour depends on TTLs of seconds to minutes; a
fair comparison needs tens of minutes of traffic per configuration. Running
the real DNSCache code against a virtual clock replays 30 minutes of traffic
in a couple of seconds, deterministically, so results are reproducible.

Workload (modelled on published DNS traces):
  * Poisson arrivals, Zipf-distributed name popularity
  * 75% A, 20% AAAA, 5% MX/TXT queries
  * 12% queries for non-existent names (half repeat-offender typos, half
    unique tracker names)
  * popularity drifts: part of the ranking is reshuffled every 10 minutes
  * flash crowds: every 2 minutes 60 clients look up one new name within
    a second (a link going viral)
  * a 3-minute upstream outage starting at two thirds of the run

Metrics per configuration:
  mean / p95 latency, cache hit ratio, upstream queries (load on the
  upstream, prefetches included - prefetching is not free), outdated
  answers (served answer != authoritative truth at that moment), and failed
  queries (SERVFAIL) - most of which happen during the outage.

    python -m adaptivedns.experiment            # prints the table
    python -m adaptivedns.experiment --json results.json
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import heapq
import itertools
import json
import random
import statistics
import time

from . import dnsmsg as d
from .cache import DNSCache, signature
from .zone import Zone, typo

LOCAL_MS = 0.35          # answering from the cache: client <-> resolver on a LAN
TIMEOUT_MS = 1500.0      # how long the resolver waits before declaring upstream dead

CONFIGS = [
    ("none", "No cache", None),
    ("basic", "Basic TTL cache (LRU)", dict(arc=False, prefetch=False, stretch=False, negative=False,
                                            serve_stale=False, coalesce=False)),
    ("proposed", "Proposed adaptive cache", dict()),
]
ABLATIONS = [
    ("no_arc", "Proposed without ARC (LRU)", dict(arc=False)),
    ("no_prefetch", "Proposed without prefetch", dict(prefetch=False)),
    ("no_stretch", "Proposed without TTL stretching", dict(stretch=False)),
    ("no_negative", "Proposed without negative caching", dict(negative=False)),
    ("no_stale", "Proposed without serve-stale", dict(serve_stale=False)),
    ("no_coalesce", "Proposed without coalescing", dict(coalesce=False)),
]


def _jitter(name, t):
    h = int.from_bytes(hashlib.blake2b(f"{name}|{int(t)}".encode(), digest_size=4).digest(), "big")
    return 0.85 + 0.4 * (h / 2**32)


def build_workload(zone: Zone, minutes=30, qps=30.0, zipf_s=0.95, nx_ratio=0.12, drift=0.25,
                   phase_minutes=10, bursts_every_s=120, burst_size=60, seed=7):
    rnd = random.Random(seed)
    names = [n for n in zone.names if not n.endswith(".edgecdn.lab")]   # users ask for sites, not edges
    order = names[:]
    rnd.shuffle(order)
    weights = [1 / (k ** zipf_s) for k in range(1, len(order) + 1)]
    cdf = list(itertools.accumulate(weights))
    typos = [typo(rnd.choice(order[:200]), rnd) for _ in range(150)]
    tw = list(itertools.accumulate(1 / (k ** 1.1) for k in range(1, len(typos) + 1)))

    t, out, phase = 0.0, [], 0
    end = minutes * 60
    while t < end:
        t += rnd.expovariate(qps)
        if int(t // (phase_minutes * 60)) != phase:
            phase = int(t // (phase_minutes * 60))
            # interest drifts: swap a fraction of ranks with random others
            for _ in range(int(len(order) * drift)):
                i, j = rnd.randrange(len(order)), rnd.randrange(len(order))
                order[i], order[j] = order[j], order[i]
        r = rnd.random()
        if r < nx_ratio:
            if rnd.random() < 0.5:
                name = typos[bisect.bisect_left(tw, rnd.random() * tw[-1])]
            else:
                name = f"x{rnd.randrange(10**7)}.trk.lab"
            qt = d.A
        else:
            name = order[bisect.bisect_left(cdf, rnd.random() * cdf[-1])]
            r2 = rnd.random()
            qt = d.A if r2 < .75 else d.AAAA if r2 < .95 else (d.MX if r2 < .98 else d.TXT)
        out.append((t, name, qt))
    # flash crowds: every few minutes a link goes viral and many clients
    # look up the same new name within a second
    for k in range(int(end // (bursts_every_s))):
        bt = (k + 0.5) * bursts_every_s
        name = order[rnd.randrange(300, len(order))]   # something that was not popular
        for _ in range(burst_size):
            out.append((bt + rnd.random() * 0.8, name, d.A))
    out.sort(key=lambda x: x[0])
    return out


def simulate(zone, workload, features=None, *, capacity=300, outage=(1200, 1380)):
    """Run one configuration. features=None means no cache at all."""
    clock = [0.0]
    cache = DNSCache(max_entries=capacity, clock=lambda: clock[0], **(features or {})) if features is not None else None
    recs = []                # (t, latency_ms, kind, outdated) kind: cache|upstream|fail
    upstream_q = prefetches = 0
    inflight = {}            # key -> completion time
    completions = []         # heap of (time, seq, key, is_prefetch, ok)
    seq = itertools.count()
    down_until = -1.0
    next_tick = 0.5

    def up(t):
        return not (outage and outage[0] <= t < outage[1])

    def wan(name, t):
        return zone.base_latency(name) * _jitter(name, t)

    def finish_until(t):
        nonlocal down_until
        while completions and completions[0][0] <= t:
            ct, _, key, pre, ok = heapq.heappop(completions)
            clock[0] = ct
            if inflight.get(key) == ct:
                inflight.pop(key, None)
            e = cache.peek(key)
            if ok:
                rc, ans, auth = zone.resolve(key[0], key[1], ct)
                cache.store(key, rc, ans, auth, 0.0, prefetch=pre)
                down_until = -1
            else:
                down_until = ct + 5
            for x in (e, cache.peek(key)):
                if x is not None:
                    x.refreshing = False

    for t, name, qt in workload:
        key = (name.lower(), qt)
        truth = signature(*zone.resolve(name, qt, t)[:2])

        if cache is None:
            upstream_q += 1
            if up(t):
                recs.append((t, wan(name, t) + LOCAL_MS, "upstream", False))
            else:
                recs.append((t, TIMEOUT_MS, "fail", False))
            continue

        while next_tick <= t:          # background work every 0.5 s
            if completions:
                finish_until(next_tick)
            clock[0] = next_tick
            for e in cache.prefetch_candidates(limit=8):
                e.refreshing = True
                upstream_q += 1
                prefetches += 1
                ok = up(next_tick)
                heapq.heappush(completions, (next_tick + (wan(e.key[0], next_tick) if ok else TIMEOUT_MS) / 1000,
                                             next(seq), e.key, True, ok))
            if next_tick % 15 == 0:
                cache.purge()
            next_tick += 0.5
        if completions:
            finish_until(t)
        clock[0] = t

        e, state = cache.lookup(key)
        if state == "fresh":
            recs.append((t, LOCAL_MS, "cache", e.signature != truth))
            continue
        if cache.features["coalesce"] and key in inflight:
            recs.append((t, (inflight[key] - t) * 1000 + LOCAL_MS, "cache", False))
            continue
        if e is not None and t < down_until and cache.can_serve_stale(e):
            recs.append((t, LOCAL_MS, "cache", e.signature != truth))
            continue

        upstream_q += 1
        if up(t):
            L = wan(name, t)
            done = t + L / 1000
            inflight[key] = done
            heapq.heappush(completions, (done, next(seq), key, False, True))
            recs.append((t, L + LOCAL_MS, "upstream", False))   # fresh from upstream: correct
        else:
            down_until = t + 5
            if e is not None and cache.can_serve_stale(e):
                recs.append((t, TIMEOUT_MS, "cache", e.signature != truth))
            else:
                recs.append((t, TIMEOUT_MS, "fail", False))

    return summarize(recs, upstream_q, prefetches, outage, cache is not None)


def summarize(recs, upstream_q, prefetches, outage, cached=True):
    n = len(recs)
    in_out = (lambda t: outage[0] <= t < outage[1]) if outage else (lambda t: False)
    normal = [r for r in recs if not in_out(r[0])]
    during = [r for r in recs if in_out(r[0])]
    lat = sorted(r[1] for r in normal)
    from_cache = [r for r in normal if r[2] == "cache"]
    return {
        "queries": n,
        "mean_ms": round(statistics.mean(lat), 2),
        "median_ms": round(lat[len(lat) // 2], 2),
        "p95_ms": round(lat[int(len(lat) * .95)], 2),
        "hit_ratio": round(len(from_cache) / len(normal), 4) if cached else 0.0,
        "upstream_queries": upstream_q,
        "upstream_per_query": round(upstream_q / n, 4),
        "prefetches": prefetches,
        "outdated_pct": round(sum(r[3] for r in normal) / len(normal) * 100, 3),
        "outdated_of_cached_pct": round(sum(r[3] for r in from_cache) / len(from_cache) * 100, 2) if from_cache else 0.0,
        "outage_queries": len(during),
        "outage_answered_pct": round(sum(r[2] != "fail" for r in during) / len(during) * 100, 1) if during else None,
        "failures": sum(r[2] == "fail" for r in recs),
    }


def run(minutes=30, qps=30.0, capacity=300, ablations=True, seed=7, zone=None, progress=None):
    zone = zone or Zone()
    wl = build_workload(zone, minutes=minutes, qps=qps, seed=seed)
    configs = CONFIGS + (ABLATIONS if ablations else [])
    outage = (minutes * 60 * 2 / 3, minutes * 60 * 2 / 3 + 180) if minutes >= 6 else None
    rows = []
    t0 = time.perf_counter()
    for i, (cid, label, feats) in enumerate(configs):
        r = simulate(zone, wl, feats, capacity=capacity, outage=outage)
        r.update(id=cid, label=label, group="ablation" if (cid, label, feats) in ABLATIONS else "main")
        rows.append(r)
        if progress:
            progress(i + 1, len(configs))
    return {
        "params": {"minutes": minutes, "qps": qps, "capacity": capacity, "queries": len(wl),
                   "names": len(zone.names), "outage_s": list(outage) if outage else None, "seed": seed},
        "rows": rows, "elapsed_s": round(time.perf_counter() - t0, 2),
    }


def print_table(res):
    p = res["params"]
    print(f"\n{p['queries']:,} queries over {p['minutes']} min ({p['qps']} q/s), "
          f"{p['names']} names, cache of {p['capacity']} entries, upstream outage at "
          f"{p['outage_s'][0] / 60:.0f}-{p['outage_s'][1] / 60:.0f} min\n" if p["outage_s"] else "")
    hdr = (f"{'Configuration':<36}{'Mean ms':>9}{'p95 ms':>9}{'Hit %':>8}{'Upstream q':>12}"
           f"{'Outdated*':>11}{'Outage ok':>11}")
    print(hdr)
    print("-" * len(hdr))
    for r in res["rows"]:
        if r["group"] == "ablation" and r["id"] == "no_arc":
            print("-" * len(hdr))
        oa = "-" if r["outage_answered_pct"] is None else f"{r['outage_answered_pct']:.0f}%"
        print(f"{r['label']:<36}{r['mean_ms']:>9.1f}{r['p95_ms']:>9.1f}{r['hit_ratio'] * 100:>8.1f}"
              f"{r['upstream_queries']:>12,}{r['outdated_of_cached_pct']:>10.1f}%{oa:>11}")
    print("\nLatency and hit ratio exclude the outage window. *Outdated: share of answers served from the cache")
    print("that no longer matched the authoritative data (inherent to TTL caching). Outage ok: queries answered")
    print(f"while the upstream was down. ({res['elapsed_s']} s to simulate)")


def main():
    ap = argparse.ArgumentParser(prog="python -m adaptivedns.experiment")
    ap.add_argument("--minutes", type=int, default=30)
    ap.add_argument("--qps", type=float, default=30)
    ap.add_argument("--capacity", type=int, default=300)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--no-ablations", action="store_true")
    ap.add_argument("--json")
    a = ap.parse_args()
    res = run(a.minutes, a.qps, a.capacity, not a.no_ablations, a.seed)
    print_table(res)
    if a.json:
        with open(a.json, "w") as f:
            json.dump(res, f, indent=2)
        print(f"saved {a.json}")


if __name__ == "__main__":
    main()
