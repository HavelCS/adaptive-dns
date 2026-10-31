"""
The caching DNS resolver (UDP + TCP on the same port).

    client (nslookup / dig / OS)  --UDP-->  [ AdaptiveDNS :8053 ]  --UDP-->  upstream resolver(s)
                                                  |
                                              DNSCache

Decision for each question:

    name on the block list          -> NXDOMAIN          BLOCKED
    fresh answer in cache           -> answer            HIT / NEG_HIT
       (kept fresh by a prefetch)                        PREFETCHED
    same question already upstream  -> wait for it       COALESCED
    ask upstream
       success                      -> cache + answer    MISS
       all upstreams failed
          expired copy available    -> stale answer      STALE      (RFC 8767)
          nothing                   -> SERVFAIL          SERVFAIL
"""
from __future__ import annotations

import asyncio
import fnmatch
import logging
import struct
import time

from . import dnsmsg as d
from .cache import DNSCache, signature
from .stats import Stats
from .upstream import UpstreamClient, UpstreamError

log = logging.getLogger("adaptivedns")


class Resolver:
    def __init__(self, cache: DNSCache, stats: Stats, upstream: UpstreamClient, host="127.0.0.1", port=8053,
                 blocklist=(), truth=None):
        self.cache, self.stats, self.upstream = cache, stats, upstream
        self.host, self.port = host, port
        self.blocklist = list(blocklist)
        self.truth = truth              # demo zone, lets us verify answers are current
        self.inflight: dict[tuple, asyncio.Future] = {}
        self._udp = self._tcp = None
        self._tasks = set()
        self.last_trace = None
        self.down_until = 0.0           # upstream recently failed: answer stale immediately
        self.last_probe = 0.0           # while down, still let one real query through per second

    async def start(self):
        loop = asyncio.get_running_loop()
        outer = self

        class UDP(asyncio.DatagramProtocol):
            def connection_made(self, t):
                self.t = t

            def datagram_received(self, data, addr):
                async def go():
                    resp = await outer.handle(data, f"{addr[0]}:{addr[1]}", "udp")
                    if resp:
                        self.t.sendto(resp, addr)
                outer._spawn(go())

        self._udp, _ = await loop.create_datagram_endpoint(UDP, local_addr=(self.host, self.port))
        self.port = self._udp.get_extra_info("sockname")[1]
        self._tcp = await asyncio.start_server(self._tcp_client, self.host, self.port)
        self._spawn(self._maintenance())
        return self

    async def close(self):
        for t in list(self._tasks):
            t.cancel()
        if self._udp:
            self._udp.close()
        if self._tcp:
            self._tcp.close()

    def _spawn(self, coro):
        t = asyncio.ensure_future(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t

    async def _tcp_client(self, reader, writer):
        peer = writer.get_extra_info("peername")
        try:
            while True:
                hdr = await asyncio.wait_for(reader.readexactly(2), 30)
                data = await reader.readexactly(struct.unpack("!H", hdr)[0])
                resp = await self.handle(data, f"{peer[0]}:{peer[1]}", "tcp")
                if resp:
                    writer.write(struct.pack("!H", len(resp)) + resp)
                    await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()

    # ------------------------------------------------------------------
    async def handle(self, data: bytes, client="", proto="udp") -> bytes | None:
        t0 = time.perf_counter()
        try:
            q = d.parse(data)
        except d.DNSError:
            if len(data) >= 2:  # we can at least echo the ID with FORMERR
                return struct.pack("!HH4H", int.from_bytes(data[:2], "big"), 0x8001, 0, 0, 0, 0)
            return None
        if q.qr:
            return None
        if q.opcode != 0:
            return d.reply_to(q, d.NOTIMP).to_bytes()
        if len(q.questions) != 1:
            return d.reply_to(q, d.FORMERR).to_bytes()

        resp, info = await self.resolve(q)
        ms = (time.perf_counter() - t0) * 1000
        limit = None if proto == "tcp" else (q.edns_size or 512)
        wire = resp.to_bytes(limit)

        qu = q.questions[0]
        outdated = None
        if self.truth is not None and resp.rcode in (d.NOERROR, d.NXDOMAIN) and info["outcome"] != "BLOCKED":
            rc, ans, _ = self.truth.resolve(qu.name, qu.qtype, time.time())
            outdated = signature(rc, ans) != signature(resp.rcode, resp.answers)
        main = [r.text for r in resp.answers if r.rtype == qu.qtype]
        answer = ", ".join(main[:2]) if main else (d.RCODES.get(resp.rcode, "") if resp.rcode else "NODATA")
        self.stats.record(name=qu.name.lower(), qtype=d.type_name(qu.qtype), outcome=info["outcome"],
                          rcode=d.RCODES.get(resp.rcode, str(resp.rcode)), ms=ms,
                          saved_ms=info.get("saved_ms", 0), answer=answer, client=client, proto=proto,
                          note=info.get("note", ""), outdated=outdated)
        info.update(total_ms=round(ms, 3), wire_bytes=len(wire), outdated=outdated)
        self.last_trace = {"query": q.summary(), "response": resp.summary(), "info": info,
                           "wire_hex": wire[:96].hex()}
        return wire

    async def resolve(self, q: d.Message):
        qu = q.questions[0]
        key = qu.key
        r = d.reply_to(q)
        steps = [("client", "Query received", 0.0)]
        t0 = time.perf_counter()

        def step(what):
            steps.append(("proxy", what, round((time.perf_counter() - t0) * 1000, 3)))

        if self._blocked(key[0]):
            r.rcode = d.NXDOMAIN
            step("Name is on the block list")
            return r, {"outcome": "BLOCKED", "steps": steps, "note": "blocked"}

        entry, state = self.cache.lookup(key)
        if state == "fresh":
            r.rcode = entry.rcode
            r.answers, r.authority = self.cache.answer_records(entry)
            if entry.negative:
                outcome = "NEG_HIT"
            elif entry.warmed:
                outcome = "PREFETCHED"
            else:
                outcome = "HIT"
            step(f"Found in cache, {int(entry.remaining(self.cache.clock()))} s left")
            self._maybe_prefetch()
            return r, {"outcome": outcome, "steps": steps, "saved_ms": entry.fetch_ms,
                       "note": f"stretched x{entry.stretch}" if entry.stretch > 1 else ""}

        step("Not in cache" if entry is None else "Cached answer has expired")

        if entry is not None and time.time() < self.down_until and self.cache.can_serve_stale(entry):
            # upstream is known to be down: don't make the user wait for another timeout
            r.rcode = entry.rcode
            r.answers, r.authority = self.cache.answer_records(entry, stale=True)
            step("Upstream marked down, served expired answer at once (serve-stale)")
            if not entry.refreshing:
                entry.refreshing = True
                self._spawn(self._refresh(entry))
            return r, {"outcome": "STALE", "steps": steps, "saved_ms": entry.fetch_ms,
                       "note": "upstream down, served stale"}

        if (entry is None and time.time() < self.down_until and key not in self.inflight
                and time.time() - self.last_probe < 1.0):
            # nothing cached and upstream known to be down: fail fast instead of
            # making the client wait for another timeout; one query a second still
            # goes through as a probe, so recovery is noticed within about a second
            r.rcode = d.SERVFAIL
            step("Upstream marked down and nothing cached: failed fast")
            return r, {"outcome": "SERVFAIL", "steps": steps, "note": "upstream down (fast fail)"}

        if self.cache.features["coalesce"] and key in self.inflight:
            step("Same question already in flight, waiting")
            try:
                m, ms = await asyncio.shield(self.inflight[key])
                r.rcode, r.answers, r.authority = m.rcode, m.answers, m.authority
                step("Shared the in-flight answer")
                return r, {"outcome": "COALESCED", "steps": steps, "saved_ms": ms}
            except Exception:
                pass

        fut = asyncio.get_running_loop().create_future()
        self.inflight[key] = fut
        self.last_probe = time.time()
        try:
            self.stats.upstream_sent()
            m, ms, server = await self.upstream.query(qu.name, qu.qtype)
            fut.set_result((m, ms))
            self.down_until = 0.0
            step(f"Asked upstream {server}: {d.RCODES.get(m.rcode, m.rcode)} in {ms:.0f} ms")
            e = self.cache.store(key, m.rcode, m.answers, m.authority, ms)
            r.rcode, r.answers, r.authority = m.rcode, m.answers, m.authority
            note = ("cached for %ds%s" % (e.ttl, f" (x{e.stretch} stretch)" if e.stretch > 1 else "")) if e else "not cached"
            step(note[0].upper() + note[1:])
            return r, {"outcome": "MISS", "steps": steps, "upstream_ms": round(ms, 1), "note": note}
        except UpstreamError as err:
            self.stats.upstream_failures += 1
            self.down_until = time.time() + 5
            fut.set_exception(err)
            fut.exception()
            step(f"Upstream failed: {err}")
            if entry is not None and self.cache.can_serve_stale(entry):
                r.rcode = entry.rcode
                r.answers, r.authority = self.cache.answer_records(entry, stale=True)
                step("Served the expired answer instead (serve-stale)")
                return r, {"outcome": "STALE", "steps": steps, "saved_ms": entry.fetch_ms,
                           "note": "upstream down, served stale"}
            r.rcode = d.SERVFAIL
            return r, {"outcome": "SERVFAIL", "steps": steps, "note": "upstream down"}
        finally:
            self.inflight.pop(key, None)

    def _blocked(self, name):
        return any(fnmatch.fnmatch(name, p) or name.endswith("." + p.lstrip("*.")) for p in self.blocklist)

    # -- background work --------------------------------------------------
    def _maybe_prefetch(self):
        for e in self.cache.prefetch_candidates(limit=8):
            e.refreshing = True
            self._spawn(self._refresh(e))

    async def _refresh(self, e):
        try:
            self.stats.upstream_sent(prefetch=True)
            m, ms, _ = await self.upstream.query(e.key[0], e.key[1])
            self.cache.store(e.key, m.rcode, m.answers, m.authority, ms, prefetch=True)
            self.down_until = 0.0
        except UpstreamError:
            self.stats.upstream_failures += 1
            self.down_until = time.time() + 5
        finally:
            cur = self.cache.peek(e.key)
            if cur is not None:
                cur.refreshing = False
            e.refreshing = False

    async def _maintenance(self):
        while True:
            await asyncio.sleep(0.5)
            try:
                self._maybe_prefetch()
                self.cache.purge()
            except Exception:
                log.exception("maintenance failed")
