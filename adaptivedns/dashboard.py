"""
Dashboard web server (HTTP, asyncio, no frameworks).

    GET  /                      UI
    GET  /api/stream            Server-Sent Events: one state snapshot per second
    POST /api/lookup            {"name","type"} - resolve through the resolver, return a trace
    POST /api/features          {"arc": true, "prefetch": false, ...}
    POST /api/config            {"max_entries", "blocklist"}
    POST /api/outage            {"on": true}   simulated outage (works with real upstreams too)
    POST /api/cache/expire      make every cached answer expire now (demo helper)
    POST /api/traffic/start     {"seconds", "rate"}
    POST /api/traffic/stop
    POST /api/cache/clear | /api/cache/delete {"name","type"} | /api/stats/reset
    POST /api/experiment        {"minutes","qps","capacity"} - run the evaluation
    GET  /api/experiment        latest evaluation result / progress
"""
from __future__ import annotations

import asyncio
import json
import mimetypes
import time
from pathlib import Path

from . import dnsmsg as d
from . import experiment
from .traffic import REAL_DOMAINS, TrafficGenerator

STATIC = Path(__file__).parent / "static"
REASONS = {200: "OK", 400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed", 500: "Server Error"}


class Dashboard:
    def __init__(self, resolver, host="127.0.0.1", port=8081, sim=None, zone=None):
        self.r = resolver
        self.host, self.port = host, port
        self.sim, self.zone = sim, zone
        self.traffic: TrafficGenerator | None = None
        self.experiment = {"state": "idle", "progress": 0, "result": None}
        self._server = None
        self.examples = self._examples()

    def _examples(self):
        if not self.zone:
            return ["example.com", "wikipedia.org", "github.com", "this-name-does-not-exist-1234.com"]
        z, picks = self.zone, {}
        for n, dom in z.domains.items():
            if n.endswith(".edgecdn.lab"):
                continue
            kind = "alias" if dom.target else dom.profile
            picks.setdefault(kind, n)
        out = [picks[k] for k in ("stable", "alias", "cdn", "volatile", "short") if k in picks]
        return out + ["gogle.lab"]

    async def start(self):
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    # ------------------------------------------------------------------
    def state(self, after=0, full=True):
        r = self.r
        return {
            "now": time.time(),
            "resolver": {"host": r.host, "port": r.port, "upstreams": list(r.upstream.health.keys()),
                         "health": r.upstream.health, "blocklist": r.blocklist,
                         "down": time.time() < r.down_until},
            "demo": self.sim is not None,
            "examples": self.examples,
            "outage": bool(self.sim.outage if self.sim else self.r.upstream.blackhole),
            "summary": r.stats.summary(),
            "cache": r.cache.describe(),
            "series": r.stats.series(90),
            "log": r.stats.recent(after, 60),
            "entries": r.cache.snapshot(150) if full else None,
            "traffic": self.traffic.status() if self.traffic else {"running": False},
            "experiment": {"state": self.experiment["state"], "progress": self.experiment["progress"]},
        }

    async def _handle(self, reader, writer):
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 15)
            lines = head.decode("latin-1").split("\r\n")
            method, path, _ = lines[0].split(" ", 2)
            headers = {k.strip().lower(): v.strip() for k, v in (l.split(":", 1) for l in lines[1:] if ":" in l)}
            body = await reader.readexactly(int(headers.get("content-length", 0) or 0))
        except Exception:
            writer.close()
            return
        path = path.split("?")[0]
        try:
            if method == "GET" and path == "/api/stream":
                await self._stream(writer)
                return
            status, ctype, payload = await self._route(method, path, body)
        except ValueError as e:
            status, ctype, payload = self._json({"error": str(e)}, 400)
        except Exception as e:
            status, ctype, payload = self._json({"error": f"{type(e).__name__}: {e}"}, 500)
        writer.write(f"HTTP/1.1 {status} {REASONS.get(status, '')}\r\nContent-Type: {ctype}\r\n"
                     f"Content-Length: {len(payload)}\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n".encode()
                     + payload)
        try:
            await writer.drain()
        finally:
            writer.close()

    @staticmethod
    def _json(obj, status=200):
        return status, "application/json", json.dumps(obj).encode()

    async def _route(self, method, path, body):
        if method == "GET":
            if path in ("/", "/index.html"):
                path = "/index.html"
            if path == "/api/state":
                return self._json(self.state())
            if path == "/api/experiment":
                return self._json(self.experiment)
            f = (STATIC / path.lstrip("/")).resolve()
            if STATIC.resolve() in f.parents and f.is_file():
                ct = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
                if ct.startswith("text/") or "javascript" in ct:
                    ct += "; charset=utf-8"
                return 200, ct, f.read_bytes()
            return 404, "text/plain", b"not found"
        if method != "POST":
            return 405, "text/plain", b"method not allowed"

        data = json.loads(body or b"{}")
        cache = self.r.cache
        if path == "/api/lookup":
            return self._json(await self._lookup(data.get("name", ""), data.get("type", "A")))
        if path == "/api/features":
            for k, v in data.items():
                cache.set_feature(k, v)
            return self._json({"ok": True, "features": cache.features})
        if path == "/api/config":
            if data.get("max_entries"):
                cache.set_policy(cache.policy.name, int(data["max_entries"]))
            if "blocklist" in data:
                self.r.blocklist = [b.strip().lower() for b in data["blocklist"] if b.strip()]
            return self._json({"ok": True})
        if path == "/api/outage":
            on = bool(data.get("on"))
            if self.sim:
                self.sim.outage = on                 # the simulated server stops answering
            else:
                self.r.upstream.blackhole = on       # real servers: cut our side of the link
            return self._json({"ok": True, "outage": on})
        if path == "/api/cache/expire":
            # demo helper: make every cached answer expire now (as if its timer ran out)
            now = time.time()
            for e in self.r.cache.entries.values():
                e.stored_at = now - e.ttl - 1
                e.refreshing = False
            return self._json({"ok": True, "expired": len(self.r.cache.entries)})
        if path == "/api/traffic/start":
            if not (self.traffic and self.traffic.running):
                names = self.zone.names if self.zone else REAL_DOMAINS
                names = [n for n in names if not n.endswith(".edgecdn.lab")]
                self.traffic = TrafficGenerator(("127.0.0.1", self.r.port), names, rate=float(data.get("rate", 20)))
                self.traffic.start(int(data.get("seconds", 90)))
            return self._json({"ok": True})
        if path == "/api/traffic/stop":
            if self.traffic:
                self.traffic.stop()
            return self._json({"ok": True})
        if path == "/api/cache/clear":
            cache.clear()
            return self._json({"ok": True})
        if path == "/api/cache/delete":
            return self._json({"ok": cache.remove((data["name"].lower(), d.parse_type(data["type"])))})
        if path == "/api/stats/reset":
            self.r.stats.reset()
            return self._json({"ok": True})
        if path == "/api/experiment":
            if self.experiment["state"] != "running":
                asyncio.ensure_future(self._run_experiment(data))
            return self._json({"ok": True})
        return 404, "text/plain", b"not found"

    async def _lookup(self, name, qtype):
        name = name.strip().rstrip(".").lower()
        if "://" in name:
            name = name.split("://", 1)[1]
        name = name.split("/")[0]
        if not name:
            raise ValueError("Enter a domain name, for example example.com")
        q = d.make_query(name, qtype)
        t0 = time.perf_counter()
        wire = await self.r.handle(q.to_bytes(), "dashboard", "udp")
        ms = (time.perf_counter() - t0) * 1000
        resp = d.parse(wire)
        trace = dict(self.r.last_trace or {})
        trace.update(ms=round(ms, 3), query_bytes=len(q.to_bytes()), response_bytes=len(wire),
                     query_hex=q.to_bytes().hex(), response=resp.summary(), response_hex=wire.hex())
        return trace

    async def _run_experiment(self, data):
        self.experiment = {"state": "running", "progress": 0, "result": None}

        def progress(i, n):
            self.experiment["progress"] = round(i / n, 2)

        loop = asyncio.get_running_loop()
        try:
            res = await loop.run_in_executor(None, lambda: experiment.run(
                minutes=int(data.get("minutes", 20)), qps=float(data.get("qps", 25)),
                capacity=int(data.get("capacity", 300)), ablations=True, zone=self.zone, progress=progress))
            self.experiment = {"state": "done", "progress": 1, "result": res}
        except Exception as e:
            self.experiment = {"state": "error", "progress": 0, "result": None, "error": str(e)}

    async def _stream(self, writer):
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-store\r\n\r\n")
        last = max(0, self.r.stats.seq - 40)
        tick = 0
        try:
            while True:
                if last > self.r.stats.seq:
                    last = 0
                st = self.state(last, full=tick % 2 == 0)
                if st["log"]:
                    last = st["log"][-1]["id"]
                writer.write(b"data: " + json.dumps(st).encode() + b"\n\n")
                await writer.drain()
                tick += 1
                await asyncio.sleep(1)
        except Exception:
            pass
        finally:
            writer.close()
