"""
Asynchronous DNS client used to talk to upstream servers.

Defences against cache poisoning (the Kaminsky attack) built in:
  * a fresh random 16-bit transaction ID for every query
  * a fresh random source port for every query (new UDP socket each time)
  * the reply must come from the server we asked, carry our ID, and echo
    the exact question - anything else is ignored
Failover: servers are tried in order; a timeout moves on to the next one.
Truncated (TC) UDP replies are retried over TCP.
"""
from __future__ import annotations

import asyncio
import random
import struct
import time

from . import dnsmsg as d


class UpstreamError(Exception):
    pass


class _Once(asyncio.DatagramProtocol):
    def __init__(self, server, qid, question, fut):
        self.server, self.qid, self.q, self.fut = server, qid, question, fut

    def datagram_received(self, data, addr):
        if self.fut.done() or addr[0] != self.server[0] or addr[1] != self.server[1]:
            return
        try:
            m = d.parse(data)
        except d.DNSError:
            return
        if m.id != self.qid or not m.qr or not m.questions:
            return
        q = m.questions[0]
        if q.name.lower().rstrip(".") != self.q.name.lower().rstrip(".") or q.qtype != self.q.qtype:
            return
        self.fut.set_result(m)

    def error_received(self, exc):
        if not self.fut.done():
            self.fut.set_exception(UpstreamError(str(exc)))


class UpstreamClient:
    def __init__(self, servers, timeout=1.5):
        self.servers = [self._parse(s) for s in servers]
        self.timeout = timeout
        self.health = {f"{h}:{p}": {"ok": 0, "fail": 0, "last_ms": None} for h, p in self.servers}
        # demo switch: behave as if the network to the upstream is cut (works with real servers too)
        self.blackhole = False

    @staticmethod
    def _parse(s):
        if isinstance(s, tuple):
            return s
        host, _, port = s.rpartition(":") if s.count(":") == 1 else (s, "", "")
        return (host or s, int(port) if port else 53)

    async def _udp(self, server, q: d.Message):
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        tr, _ = await loop.create_datagram_endpoint(lambda: _Once(server, q.id, q.questions[0], fut),
                                                    remote_addr=server)
        try:
            tr.sendto(q.to_bytes())
            return await asyncio.wait_for(fut, self.timeout)
        finally:
            tr.close()

    async def _tcp(self, server, q: d.Message):
        r, w = await asyncio.wait_for(asyncio.open_connection(*server), self.timeout)
        try:
            b = q.to_bytes()
            w.write(struct.pack("!H", len(b)) + b)
            await w.drain()
            ln = struct.unpack("!H", await asyncio.wait_for(r.readexactly(2), self.timeout))[0]
            m = d.parse(await asyncio.wait_for(r.readexactly(ln), self.timeout))
            if m.id != q.id:
                raise UpstreamError("TCP reply ID mismatch")
            return m
        finally:
            w.close()

    async def query(self, name: str, qtype: int) -> tuple[d.Message, float, str]:
        if self.blackhole:
            await asyncio.sleep(self.timeout)       # a real outage looks like a timeout
            for label in self.health:
                self.health[label]["fail"] += 1
            raise UpstreamError("all upstream servers failed (simulated outage)")
        last = None
        for server in self.servers:
            label = f"{server[0]}:{server[1]}"
            q = d.make_query(name, qtype, qid=random.randrange(65536))
            t0 = time.perf_counter()
            try:
                m = await self._udp(server, q)
                if m.tc:
                    m = await self._tcp(server, q)
                ms = (time.perf_counter() - t0) * 1000
                self.health[label].update(ok=self.health[label]["ok"] + 1, last_ms=round(ms, 1))
                return m, ms, label
            except (asyncio.TimeoutError, OSError, UpstreamError, d.DNSError, asyncio.IncompleteReadError) as e:
                self.health[label]["fail"] += 1
                last = e
        raise UpstreamError(f"all upstream servers failed ({type(last).__name__})")
