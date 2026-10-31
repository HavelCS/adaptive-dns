"""
A tiny dig-like client, so the demo works even where nslookup/dig are missing.

    python -m adaptivedns.query example.lab
    python -m adaptivedns.query example.lab AAAA --server 127.0.0.1:8053
"""
import argparse
import asyncio

from . import dnsmsg as d
from .upstream import UpstreamClient, UpstreamError


async def main():
    ap = argparse.ArgumentParser(prog="python -m adaptivedns.query")
    ap.add_argument("name")
    ap.add_argument("type", nargs="?", default="A")
    ap.add_argument("--server", default="127.0.0.1:8053")
    a = ap.parse_args()
    c = UpstreamClient([a.server], timeout=3)
    try:
        m, ms, srv = await c.query(a.name, d.parse_type(a.type))
    except UpstreamError as e:
        print(f";; no answer from {a.server}: {e}")
        return
    print(f";; ->>HEADER<<- opcode: QUERY, status: {d.RCODES.get(m.rcode, m.rcode)}, id: {m.id}")
    print(f";; flags: {' '.join(f.lower() for f in m.flag_names())}; "
          f"QUERY: {len(m.questions)}, ANSWER: {len(m.answers)}, AUTHORITY: {len(m.authority)}\n")
    print(";; QUESTION SECTION:")
    for q in m.questions:
        print(f";{q.name}.\t\tIN\t{d.type_name(q.qtype)}")
    for title, sec in (("ANSWER", m.answers), ("AUTHORITY", m.authority)):
        if sec:
            print(f"\n;; {title} SECTION:")
            for r in sec:
                print(f"{r.name}.\t{r.ttl}\tIN\t{d.type_name(r.rtype)}\t{r.text}")
    print(f"\n;; Query time: {ms:.2f} msec\n;; SERVER: {srv}")


if __name__ == "__main__":
    asyncio.run(main())
