"""
Side-by-side demo: the same lookup patterns, first with the full algorithm,
then with a basic cache (all six fixes off).

Start the resolver first, in another terminal, either with the simulated internet:

    python -m adaptivedns --demo --open

or with a real upstream such as Google DNS:

    python -m adaptivedns --upstream 8.8.8.8 --open

then run:

    python -m adaptivedns.compare              # all five patterns
    python -m adaptivedns.compare --list       # just print the domain names used
    python -m adaptivedns.compare typos crowd  # only some patterns

Patterns (each one targets one fix):
    typos    the same misspelt names asked again and again   -> negative caching
    crowd    25 users ask for one new name at the same moment -> query coalescing
    scan     favourites, then a flood of one-off names        -> ARC two-shelf memory
    hot      a popular name with a 5-second timer, every 0.5 s -> prefetch
    outage   names asked, timers run out, upstream goes down   -> serve-stale

The queries are real DNS packets sent to the resolver, so the dashboard shows
them live. Settings are restored to the full algorithm at the end.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
import urllib.request

from . import dnsmsg as d
from .upstream import UpstreamClient, UpstreamError
from .traffic import REAL_DOMAINS
from .zone import Zone

UI = "http://127.0.0.1:8081"
DNS = ("127.0.0.1", 8053)
FULL = dict(arc=True, prefetch=True, stretch=True, negative=True, serve_stale=True, coalesce=True)
BASIC = {k: False for k in FULL}
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def api(path, body=None):
    # /api/state is read with GET; every other call is a POST
    if body is None and path != "/api/state":
        body = {}
    req = urllib.request.Request(UI + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with _opener.open(req, timeout=30) as r:
        return json.loads(r.read() or b"{}")


def real_names():
    """Real domains, used when the resolver forwards to a real server such as 8.8.8.8."""
    R = REAL_DOMAINS
    return {
        "typos": ["gooogle-xq7.com", "yootube-zz9.com", "wikipeedia-q1x.org"],   # do not exist
        "crowd": "kaggle.com",
        "scan_favourites": R[:8],
        "scan_flood": R[20:80],
        "hot": [],
        "outage": R[100:105],
    }


def names(real=False):
    """Fixed names from the simulated internet (same on every computer)."""
    if real:
        return real_names()
    z = Zone()
    by = {}
    for n, dom in z.domains.items():
        if n.endswith(".edgecdn.lab"):
            continue
        by.setdefault("alias" if dom.target else dom.profile, []).append(n)
    stable, short = by["stable"], by["short"]
    return {
        "typos": ["gogle.lab", "yotube.lab", "faceboook.test"],
        "crowd": stable[100],
        "scan_favourites": stable[:8],
        "scan_flood": stable[200:260],
        "hot": short[:2],
        "outage": short[10:15],
    }


class Runner:
    def __init__(self):
        self.client = UpstreamClient([DNS], timeout=4.0)

    async def ask(self, name, qtype=d.A):
        t0 = time.perf_counter()
        try:
            m, _, _ = await self.client.query(name, qtype)
            rc = d.RCODES.get(m.rcode, str(m.rcode))
        except UpstreamError:
            rc = "NO REPLY"
        return rc, (time.perf_counter() - t0) * 1000

    def snapshot(self):
        s = api("/api/state")["summary"]
        return s["outcomes"], s["upstream_queries"]

    async def measure(self, coro_factory):
        """Run a pattern and return what changed in the resolver's counters."""
        o1, u1 = self.snapshot()
        results = await coro_factory()
        await asyncio.sleep(0.3)
        o2, u2 = self.snapshot()
        delta = {k: o2[k] - o1[k] for k in o2}
        return results, delta, u2 - u1


def fmt_row(label, lookups, cached, waited, upstream, avg_ms, failed):
    return f"  {label:<16}{lookups:>8}{cached:>13}{waited:>13}{upstream:>15}{avg_ms:>11.1f}{failed:>8}"


HEADER = (f"  {'':<16}{'Lookups':>8}{'From memory':>13}{'Had to wait':>13}{'Sent upstream':>15}"
          f"{'Avg ms':>11}{'Failed':>8}")
CACHED = ("HIT", "PREFETCHED", "NEG_HIT", "COALESCED", "STALE")


async def run_pattern(r: Runner, key: str, N: dict, mode: str):
    """Returns (rows_for_table, note). Cache is cleared before each run."""
    api("/api/cache/clear")
    if key == "typos":
        async def go():
            out = []
            for _ in range(4):
                for n in N["typos"]:
                    out.append(await r.ask(n))
            return out
    elif key == "crowd":
        async def go():
            return await asyncio.gather(*(r.ask(N["crowd"]) for _ in range(25)))
    elif key == "scan":
        api("/api/config", {"max_entries": 20})

        async def go():
            for _ in range(2):                      # favourites, seen twice
                for n in N["scan_favourites"]:
                    await r.ask(n)
            for n in N["scan_flood"]:               # 60 one-off names
                await r.ask(n)
            return [await r.ask(n) for n in N["scan_favourites"]]  # favourites again
    elif key == "hot":
        async def go():
            out = []
            for _ in range(24):                     # 12 seconds
                for n in N["hot"]:
                    out.append(await r.ask(n))
                await asyncio.sleep(0.5)
            return out
    elif key == "outage":
        async def go():
            for n in N["outage"]:
                await r.ask(n)
            # make the cached answers expire now, as if their timers had run out
            api("/api/cache/expire")
            await asyncio.sleep(0.6)
            api("/api/outage", {"on": True})
            try:
                return [await r.ask(n) for n in N["outage"]]
            finally:
                api("/api/outage", {"on": False})
                await asyncio.sleep(1.5)            # let the resolver notice the upstream is back
    else:
        raise ValueError(key)

    # for "scan" and "outage" only the final phase is scored, so measure that phase alone
    if key in ("scan", "outage"):
        o1, u1 = r.snapshot()
        results = await go()
        await asyncio.sleep(0.3)
        o2, u2 = r.snapshot()
        # the counters include warm-up; score the final phase from the answers themselves
        delta = {k: o2[k] - o1[k] for k in o2}
        up = None
    else:
        results, delta, up = await r.measure(go)
    if key == "scan":
        api("/api/config", {"max_entries": 300})

    n = len(results)
    avg = sum(ms for _, ms in results) / n
    waited = sum(1 for _, ms in results if ms > 5)
    failed = sum(1 for rc, _ in results if rc in ("SERVFAIL", "NO REPLY"))
    if key == "scan":
        cached = sum(1 for _, ms in results if ms < 5)
        upstream = n - cached
    elif key == "outage":
        cached = n - failed
        upstream = "-"
    else:
        cached = sum(delta.get(k, 0) for k in CACHED)
        upstream = up
    return n, cached, waited, upstream, avg, failed


TITLES = {
    "typos": ("Repeated typos", "negative caching",
              "3 misspelt names, each asked 4 times."),
    "crowd": ("Flash crowd", "query coalescing",
              "25 users ask for the same new name at the same moment."),
    "scan": ("Favourites vs a flood", "ARC two-shelf memory",
             "Cache shrunk to 20 entries. 8 favourite names asked twice, then 60 one-off names,\n"
             "  then the 8 favourites again. Scored: the last 8 lookups."),
    "hot": ("Popular short-timer names", "prefetch",
            "2 names with a 5-second timer, each asked every 0.5 s for 12 s."),
    "outage": ("Upstream outage", "serve-stale",
               "5 names asked, their timers are made to run out, then the upstream goes down\n"
               "  and the same 5 names are asked again. Scored: the last 5 lookups."),
}


NOTES = {
    "typos": "Full algorithm: each typo goes upstream once, then \"does not exist\" is answered from memory.",
    "crowd": "Same wait for everyone (all 25 wait for one answer), but the full algorithm sends 1 query instead of 25.",
    "scan": "Basic cache (LRU): the one-off names pushed the favourites out. ARC kept them on shelf 2.",
    "hot": "Full algorithm refreshes these names in the background before they expire, so almost nobody waits.\n"
           "  Its upstream count includes those background refreshes.",
    "outage": "Full algorithm serves the last known answers. Its first lookup waits 1.5 s to notice the outage,\n"
              "  then answers instantly. The basic cache has nothing to give, so every lookup fails.",
}


async def main_async(keys):
    try:
        api("/api/state")
    except Exception:
        print("Cannot reach the dashboard at http://127.0.0.1:8081.\n"
              "Start the resolver first, in another terminal:  python -m adaptivedns --demo --open")
        return
    real = not api("/api/state").get("demo")
    N = names(real)
    if real:
        print("Using real DNS (the resolver was started without --demo).")
        if "hot" in keys:
            print("Skipping 'hot': it needs names with 5-second timers, and real sites use timers of minutes.")
            keys = [k for k in keys if k != "hot"]
    r = Runner()
    try:
        for key in keys:
            title, fix, what = TITLES[key]
            print(f"\n{title}  (shows: {fix})\n  {what}\n")
            rows = {}
            for mode, feats in (("Full algorithm", FULL), ("Basic cache", BASIC)):
                api("/api/features", feats)
                print(f"    running with {mode}…", flush=True)
                rows[mode] = await run_pattern(r, key, N, mode)
            print("\n" + HEADER)
            for mode, (n, cached, waited, up, avg, failed) in rows.items():
                print(fmt_row(mode, n, cached, waited, "-" if up is None else up, avg, failed))
            print("\n  " + NOTES[key])
    finally:
        api("/api/features", FULL)
        api("/api/config", {"max_entries": 300})
        api("/api/outage", {"on": False})
    print("\nDone. Settings are back to the full algorithm.")


def main():
    ap = argparse.ArgumentParser(prog="python -m adaptivedns.compare",
                                 description="Show the full algorithm and a basic cache side by side")
    ap.add_argument("patterns", nargs="*", metavar="PATTERN",
                    help="which patterns to run: " + ", ".join(TITLES) + " (default: all)")
    ap.add_argument("--list", action="store_true", help="print the domain names each pattern uses")
    ap.add_argument("--real", action="store_true", help="with --list: show the real-DNS names")
    a = ap.parse_args()
    bad = [p for p in a.patterns if p not in TITLES]
    if bad:
        ap.error(f"unknown pattern {bad[0]!r}; choose from {', '.join(TITLES)}")
    if a.list:
        N = names(a.real)
        print("typos     :", ", ".join(N["typos"]))
        print("crowd     :", N["crowd"], "(25 times at once)")
        print("scan      : favourites", ", ".join(N["scan_favourites"]))
        print("            flood of 60 names such as", ", ".join(N["scan_flood"][:4]), "…")
        print("hot       :", ", ".join(N["hot"]) + " (5-second timers)" if N["hot"] else "(skipped with real DNS)")
        print("outage    :", ", ".join(N["outage"]))
        return
    asyncio.run(main_async(a.patterns or list(TITLES)))


if __name__ == "__main__":
    main()
