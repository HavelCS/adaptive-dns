"""
    python -m adaptivedns --demo --open

Starts the resolver (UDP+TCP 127.0.0.1:8053), the dashboard (http://127.0.0.1:8081)
and, with --demo, a simulated upstream on 127.0.0.1:9053 so everything works offline.
Without --demo the resolver forwards to real public resolvers (1.1.1.1, 8.8.8.8).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import webbrowser

from .cache import DNSCache
from .dashboard import Dashboard
from .resolver import Resolver
from .simupstream import SimUpstream
from .stats import Stats
from .upstream import UpstreamClient
from .zone import Zone


def parse_args(argv=None):
    ap = argparse.ArgumentParser(prog="adaptivedns", description="Popularity- and stability-aware caching DNS resolver")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8053, help="DNS port (53 needs admin/root)")
    ap.add_argument("--ui-port", type=int, default=8081)
    ap.add_argument("--upstream", action="append", metavar="IP[:PORT]",
                    help="upstream resolver(s), default 1.1.1.1 and 8.8.8.8")
    ap.add_argument("--max-entries", type=int, default=300)
    ap.add_argument("--basic", action="store_true", help="start with every adaptive feature off (baseline)")
    ap.add_argument("--block", action="append", default=[], metavar="DOMAIN")
    ap.add_argument("--demo", action="store_true", help="use the simulated upstream (works offline)")
    ap.add_argument("--open", action="store_true", help="open the dashboard in a browser")
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap.parse_args(argv)


async def main(argv=None):
    a = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.WARNING)
    zone = sim = None
    if a.demo:
        zone = Zone()
        sim = await SimUpstream(zone, port=9053).start()
        upstreams = [("127.0.0.1", sim.port)]
    else:
        upstreams = a.upstream or ["1.1.1.1", "8.8.8.8"]

    feats = dict(arc=False, prefetch=False, stretch=False, negative=False, serve_stale=False,
                 coalesce=False) if a.basic else {}
    cache = DNSCache(max_entries=a.max_entries, **feats)
    resolver = await Resolver(cache, Stats(), UpstreamClient(upstreams), host=a.host, port=a.port,
                              blocklist=a.block, truth=zone).start()
    ui = await Dashboard(resolver, port=a.ui_port, sim=sim, zone=zone).start()
    url = f"http://127.0.0.1:{ui.port}"
    up = ", ".join(f"{h}:{p}" for h, p in resolver.upstream.servers)
    example = zone.names[0] if zone else "example.com"
    print(f"""
  AdaptiveDNS is running
  DNS resolver  {a.host}:{resolver.port}  (UDP + TCP)
  Upstream      {up}{'  (simulated, offline demo)' if a.demo else ''}
  Dashboard     {url}

  Try:  nslookup -port={resolver.port} {example} 127.0.0.1
        dig @127.0.0.1 -p {resolver.port} {example}
        python -m adaptivedns.query {example}

  Ctrl+C to stop
""")
    if a.open:
        webbrowser.open(url)
    await asyncio.Event().wait()


def run():
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    run()
