"""
Simulated upstream recursive resolver (think "your ISP's DNS" or 8.8.8.8),
answering from the synthetic Zone with realistic WAN delays.

Used with --demo so the project works in a lab with no internet, and so
the dashboard can "pull the plug" (outage mode) to demonstrate serve-stale.
"""
from __future__ import annotations

import asyncio
import random
import struct
import time

from . import dnsmsg as d
from .zone import Zone


class SimUpstream:
    def __init__(self, zone: Zone, host="127.0.0.1", port=9053, delay_scale=1.0):
        self.zone, self.host, self.port, self.delay_scale = zone, host, port, delay_scale
        self.queries = 0
        self.outage = False
        self.rnd = random.Random()
        self._udp = None
        self._tcp = None

    def answer(self, q: d.Message) -> d.Message:
        r = d.reply_to(q)
        r.ra = True
        if not q.questions:
            r.rcode = d.FORMERR
            return r
        qu = q.questions[0]
        rc, ans, auth = self.zone.resolve(qu.name, qu.qtype, time.time())
        r.rcode, r.answers, r.authority = rc, ans, auth
        return r

    async def _respond(self, data: bytes, send):
        self.queries += 1
        if self.outage:
            return  # silently drop: the client will time out, like a dead server
        try:
            q = d.parse(data)
        except d.DNSError:
            return
        name = q.questions[0].name if q.questions else ""
        await asyncio.sleep(self.zone.latency(name, self.rnd) / 1000 * self.delay_scale)
        if self.outage:
            return
        send(self.answer(q))

    async def start(self):
        loop = asyncio.get_running_loop()
        outer = self

        class Proto(asyncio.DatagramProtocol):
            def connection_made(self, transport):
                self.t = transport

            def datagram_received(self, data, addr):
                def send(msg):
                    size = 512 if msg.edns_size is None else 1232
                    self.t.sendto(msg.to_bytes(size), addr)
                asyncio.ensure_future(outer._respond(data, send))

        self._udp, _ = await loop.create_datagram_endpoint(Proto, local_addr=(self.host, self.port))
        self.port = self._udp.get_extra_info("sockname")[1]

        async def tcp_client(reader, writer):
            try:
                ln = struct.unpack("!H", await reader.readexactly(2))[0]
                data = await reader.readexactly(ln)
                done = asyncio.get_running_loop().create_future()

                def send(msg):
                    b = msg.to_bytes()
                    writer.write(struct.pack("!H", len(b)) + b)
                    done.set_result(True)
                await outer._respond(data, send)
                if done.done():
                    await writer.drain()
            except Exception:
                pass
            finally:
                writer.close()

        self._tcp = await asyncio.start_server(tcp_client, self.host, self.port)
        return self

    async def close(self):
        if self._udp:
            self._udp.close()
        if self._tcp:
            self._tcp.close()
