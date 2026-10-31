"""Run with:  python -m unittest discover -s tests -v"""
import asyncio
import struct
import unittest

from adaptivedns import dnsmsg as d
from adaptivedns import experiment
from adaptivedns.cache import DNSCache
from adaptivedns.policies import ARCPolicy, LRUPolicy
from adaptivedns.resolver import Resolver
from adaptivedns.simupstream import SimUpstream
from adaptivedns.stats import Stats
from adaptivedns.upstream import UpstreamClient
from adaptivedns.zone import Zone


def A(name, ip, ttl=60):
    return d.Record(name, d.A, d.IN, ttl, d.make_rdata(d.A, ip))


def SOA(zone="lab", minimum=60, ttl=60):
    return d.Record(zone, d.SOA, d.IN, ttl, d.make_rdata(d.SOA, ("ns1." + zone, "h." + zone, 1, 1, 1, 1, minimum)))


class WireFormat(unittest.TestCase):
    def test_round_trip(self):
        m = d.reply_to(d.make_query("www.example.lab", d.A, qid=4321))
        m.answers = [d.Record("www.example.lab", d.CNAME, d.IN, 30, d.encode_name("edge.cdn.lab")),
                     A("edge.cdn.lab", "10.1.2.3", 30)]
        p = d.parse(m.to_bytes())
        self.assertEqual(p.id, 4321)
        self.assertTrue(p.qr and p.ra and p.rd)
        self.assertEqual([r.text for r in p.answers], ["edge.cdn.lab.", "10.1.2.3"])
        self.assertEqual(p.edns_size, 1232)

    def test_compression_pointer_decoding(self):
        # header + question "a.lab" + answer whose name is a pointer to offset 12
        hdr = struct.pack("!6H", 1, 0x8180, 1, 1, 0, 0)
        q = d.encode_name("a.lab") + struct.pack("!HH", 1, 1)
        ans = b"\xc0\x0c" + struct.pack("!HHIH", 5, 1, 60, 2) + b"\xc0\x0c"   # CNAME -> itself via pointer
        m = d.parse(hdr + q + ans)
        self.assertEqual(m.answers[0].name, "a.lab")
        self.assertEqual(m.answers[0].text, "a.lab.")   # rdata decompressed

    def test_pointer_loop_rejected(self):
        hdr = struct.pack("!6H", 1, 0, 1, 0, 0, 0)
        with self.assertRaises(d.DNSError):
            d.parse(hdr + b"\xc0\x0c" + b"\x00\x01\x00\x01")

    def test_truncation_sets_tc(self):
        m = d.reply_to(d.make_query("big.lab", d.A, edns=None))
        m.answers = [A("big.lab", f"10.0.{i // 250}.{i % 250 + 1}") for i in range(60)]
        wire = m.to_bytes(512)
        self.assertLessEqual(len(wire), 512)
        self.assertTrue(d.parse(wire).tc)

    def test_record_text(self):
        self.assertEqual(d.Record("x", d.MX, 1, 1, d.make_rdata(d.MX, (10, "mx.x"))).text, "10 mx.x.")
        self.assertEqual(d.Record("x", d.AAAA, 1, 1, d.make_rdata(d.AAAA, "2001:db8::1")).text, "2001:db8::1")


class Policies(unittest.TestCase):
    def test_lru(self):
        p = LRUPolicy(2)
        p.admit("a"); p.admit("b"); p.access("a")
        self.assertEqual(p.admit("c"), ["b"])

    def test_arc_scan_resistance(self):
        p, resident = ARCPolicy(4), set()
        for k in "ab":
            p.admit(k); p.access(k); resident.add(k)
        for i in range(30):
            for v in p.admit(f"s{i}"):
                resident.discard(v)
            resident.add(f"s{i}")
        self.assertTrue({"a", "b"} <= resident)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class CacheLogic(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.c = DNSCache(max_entries=10, clock=self.clock)

    def test_ttl_and_expiry(self):
        self.c.store(("a.lab", 1), 0, [A("a.lab", "1.1.1.1", 30)], [], 50)
        self.assertEqual(self.c.lookup(("a.lab", 1))[1], "fresh")
        self.clock.t += 31
        self.assertEqual(self.c.lookup(("a.lab", 1))[1], "stale")

    def test_ttl_counts_down_in_answers(self):
        e = self.c.store(("a.lab", 1), 0, [A("a.lab", "1.1.1.1", 30)], [], 50)
        self.clock.t += 10
        ans, _ = self.c.answer_records(e)
        self.assertEqual(ans[0].ttl, 20)

    def test_negative_caching_uses_soa_minimum(self):
        e = self.c.store(("nx.lab", 1), d.NXDOMAIN, [], [SOA(minimum=45, ttl=300)], 50)
        self.assertTrue(e.negative)
        self.assertEqual(e.ttl, 45)
        c2 = DNSCache(negative=False, clock=self.clock)
        self.assertIsNone(c2.store(("nx.lab", 1), d.NXDOMAIN, [], [SOA()], 50))

    def test_servfail_not_cached(self):
        self.assertIsNone(self.c.store(("x.lab", 1), d.SERVFAIL, [], [], 50))

    def test_stretch_only_for_proven_stable_answers(self):
        key = ("s.lab", 1)
        e = self.c.store(key, 0, [A("s.lab", "1.1.1.1", 10)], [], 5)
        self.assertEqual(e.ttl, 10)                          # nothing learned yet
        for _ in range(40):                                  # unchanged for 400 s
            self.clock.t += 10
            e = self.c.store(key, 0, [A("s.lab", "1.1.1.1", 10)], [], 5)
        self.assertEqual(e.ttl, 40)                          # stretched, capped at 4x
        self.clock.t += 10
        e = self.c.store(key, 0, [A("s.lab", "9.9.9.9", 10)], [], 5)   # it changed after ~410 s
        lifetime = self.c.learned[key].lifetime
        self.assertAlmostEqual(lifetime, 410, delta=1)
        self.assertLessEqual(e.ttl, 0.1 * lifetime + 0.01)              # never beyond 10% of lifetime

    def test_volatile_answers_never_stretched(self):
        key = ("v.lab", 1)
        for i in range(20):
            self.clock.t += 10
            e = self.c.store(key, 0, [A("v.lab", f"1.1.1.{i + 1}", 10)], [], 5)
        self.assertEqual(e.ttl, 10)

    def test_prefetch_picks_popular_entries_near_expiry(self):
        hot = self.c.store(("hot.lab", 1), 0, [A("hot.lab", "1.1.1.1", 100)], [], 5)
        cold = self.c.store(("cold.lab", 1), 0, [A("cold.lab", "1.1.1.2", 100)], [], 5)
        self.c.lookup(("cold.lab", 1))
        self.assertEqual(self.c.prefetch_candidates(), [])   # not close to expiry yet
        self.clock.t += 88
        for _ in range(5):                                    # recently popular
            self.c.lookup(("hot.lab", 1))
        cands = self.c.prefetch_candidates()
        self.assertIn(hot, cands)
        self.assertNotIn(cold, cands)

    def test_round_robin_reorder_is_not_a_change(self):
        key = ("rr.lab", 1)
        self.c.store(key, 0, [A("rr.lab", "1.1.1.1"), A("rr.lab", "2.2.2.2")], [], 5)
        self.c.store(key, 0, [A("rr.lab", "2.2.2.2"), A("rr.lab", "1.1.1.1")], [], 5)
        self.assertEqual(self.c.learned[key].changes, 0)


class Resolution(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.zone = Zone(n=300)
        self.up = await SimUpstream(self.zone, port=0, delay_scale=0.2).start()
        self.res = await Resolver(DNSCache(), Stats(), UpstreamClient([("127.0.0.1", self.up.port)], timeout=0.3),
                                  port=0, blocklist=["ads.lab"], truth=self.zone).start()
        self.client = UpstreamClient([("127.0.0.1", self.res.port)], timeout=3)
        self.name = next(n for n in self.zone.names if self.zone.domains[n].profile == "stable")

    async def asyncTearDown(self):
        await self.res.close()
        await self.up.close()

    def last(self):
        return self.res.stats.log[-1]["outcome"]

    async def ask(self, name, qt=d.A):
        m, _, _ = await self.client.query(name, qt)
        return m

    async def test_miss_then_hit(self):
        a = await self.ask(self.name)
        self.assertEqual(self.last(), "MISS")
        b = await self.ask(self.name)
        self.assertEqual(self.last(), "HIT")
        self.assertEqual([r.text for r in a.answers], [r.text for r in b.answers])
        self.assertEqual(self.up.queries, 1)

    async def test_nxdomain_is_negatively_cached(self):
        m = await self.ask("no-such-name.lab")
        self.assertEqual(m.rcode, d.NXDOMAIN)
        self.assertEqual(m.authority[0].rtype, d.SOA)
        await self.ask("no-such-name.lab")
        self.assertEqual(self.last(), "NEG_HIT")

    async def test_coalescing(self):
        await asyncio.gather(*(self.ask(self.zone.names[7]) for _ in range(6)))
        self.assertEqual(self.up.queries, 1)
        self.assertEqual(self.res.stats.outcomes["COALESCED"], 5)

    async def test_serve_stale_during_outage(self):
        await self.ask(self.name)
        e = self.res.cache.peek((self.name.lower(), d.A))
        e.stored_at -= 10_000                       # long expired
        self.up.outage = True
        m = await self.ask(self.name)
        self.assertEqual(m.rcode, d.NOERROR)
        self.assertEqual(self.last(), "STALE")
        self.assertEqual(m.answers[0].ttl, 30)
        m = await self.ask("never-cached.lab")
        self.assertEqual(m.rcode, d.SERVFAIL)

    async def test_blocklist(self):
        m = await self.ask("ads.lab")
        self.assertEqual(m.rcode, d.NXDOMAIN)
        self.assertEqual(self.last(), "BLOCKED")

    async def test_tcp(self):
        r, w = await asyncio.open_connection("127.0.0.1", self.res.port)
        q = d.make_query(self.name).to_bytes()
        w.write(struct.pack("!H", len(q)) + q)
        await w.drain()
        ln = struct.unpack("!H", await r.readexactly(2))[0]
        m = d.parse(await r.readexactly(ln))
        w.close()
        self.assertEqual(m.rcode, d.NOERROR)
        self.assertTrue(m.answers)

    async def test_answers_match_truth(self):
        for n in self.zone.names[:20]:
            await self.ask(n)
        self.assertEqual(self.res.stats.outdated, 0)


class Evaluation(unittest.TestCase):
    def test_proposed_beats_basic(self):
        res = experiment.run(minutes=8, qps=20, capacity=150, ablations=False, zone=Zone(n=600))
        by = {r["id"]: r for r in res["rows"]}
        self.assertLess(by["proposed"]["mean_ms"], by["basic"]["mean_ms"])
        self.assertLess(by["basic"]["mean_ms"], by["none"]["mean_ms"])
        self.assertGreater(by["proposed"]["hit_ratio"], by["basic"]["hit_ratio"])
        self.assertGreater(by["proposed"]["outage_answered_pct"], by["basic"]["outage_answered_pct"])


if __name__ == "__main__":
    unittest.main()
