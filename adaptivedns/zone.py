"""
A synthetic, deterministic "Internet" of domain names.

It drives both the simulated upstream DNS server (live demo) and the
virtual-time evaluation, so the two always agree. Every name has a
behaviour profile modelled on what real DNS looks like:

  stable    TTL 300 s, address never changes            (most sites)
  cdn       TTL 20 s, but the address only rotates every 3 minutes
            (CDNs publish very low TTLs "just in case")
  volatile  TTL 60 s, address changes every 45 s        (failover / dynamic DNS)
  alias     www.<x> is a CNAME to <x>, TTL 120 s
  short     TTL 5 s, never changes                      (aggressive low TTL)

Some names also have AAAA (IPv6) and MX records. Queries for a type that a
name does not have return NOERROR/NODATA, and names that do not exist return
NXDOMAIN - both carry an SOA record for negative caching (RFC 2308).

`rrset(name, qtype, t)` answers "what is the true answer at time t", which
lets the evaluation count how many answers a cache served that were already
out of date.
"""
from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass

from . import dnsmsg as d

SYL = ["ka", "lo", "mi", "ra", "ve", "no", "ti", "su", "pa", "zen", "bo", "qua", "dri", "fy", "lu",
       "mar", "ne", "ox", "pi", "ro", "sa", "tek", "ul", "vo", "wy", "xa", "yo", "zi", "cor", "dex"]
WORDS = ["pay", "shop", "news", "cloud", "mail", "maps", "play", "learn", "code", "photo", "chat",
         "music", "bank", "store", "travel", "food", "health", "sport", "data", "stream", "docs", "home"]
TLDS = ["lab", "lab", "lab", "test"]
PREFIXES = ["", "", "", "api.", "cdn.", "static.", "img.", "auth.", "m.", "app."]

PROFILES = {
    #           ttl  change_every_s  weight
    "stable":   (300, None, 0.40),
    "cdn":      (20, 180, 0.22),
    "volatile": (60, 45, 0.10),
    "alias":    (120, None, 0.16),
    "short":    (5, None, 0.12),
}
ZONE_SOA_MIN = 60
NEG_TTL = 60


def _h(*parts) -> int:
    return int.from_bytes(hashlib.blake2b("|".join(map(str, parts)).encode(), digest_size=8).digest(), "big")


@dataclass
class Domain:
    name: str
    profile: str
    ttl: int
    period: int | None
    phase: float
    has_aaaa: bool
    has_mx: bool
    latency_ms: float      # how long a full recursive resolution takes from here
    target: str | None = None  # for aliases


class Zone:
    def __init__(self, n=1500, seed=11):
        rnd = random.Random(seed)
        self.domains: dict[str, Domain] = {}
        names = []
        while len(names) < n:
            brand = rnd.choice(WORDS) + rnd.choice(SYL) + (rnd.choice(SYL) if rnd.random() < .5 else "")
            name = f"{rnd.choice(PREFIXES)}{brand}.{rnd.choice(TLDS)}"
            if name not in self.domains:
                names.append(name)
                self.domains[name] = None  # reserve
        profiles = list(PROFILES)
        weights = [PROFILES[p][2] for p in profiles]
        for name in names:
            prof = rnd.choices(profiles, weights)[0]
            ttl, period, _ = PROFILES[prof]
            # recursive resolution cost: most nearby, a long tail of far-away
            # authoritative servers (lognormal-ish)
            lat = 18 + 150 * rnd.random() ** 2.2 + rnd.random() * 10
            dom = Domain(name, prof, ttl, period, rnd.random() * (period or 1), rnd.random() < .35,
                         rnd.random() < .15, round(lat, 1))
            self.domains[name] = dom
            if prof == "alias":
                # name is a CNAME to an edge host on a CDN, like most big sites
                edge = f"e{_h(name) % 9000 + 1000}.edgecdn.lab"
                dom.target = edge
                if edge not in self.domains:
                    ettl, eper, _ = PROFILES["cdn"]
                    self.domains[edge] = Domain(edge, "cdn", ettl, eper, rnd.random() * eper,
                                                dom.has_aaaa, False, round(lat * 0.6, 1))
        self.names = list(self.domains)

    # -------------------------------------------------------------------
    def exists(self, name: str) -> bool:
        return name.lower().rstrip(".") in self.domains

    def version(self, dom: Domain, t: float) -> int:
        return int((t + dom.phase) // dom.period) if dom.period else 0

    def _addr4(self, dom: Domain, t: float) -> str:
        v = self.version(dom, t)
        h = _h(dom.name, v)
        return f"{10 + h % 180}.{(h >> 8) % 256}.{(h >> 16) % 256}.{1 + (h >> 24) % 254}"

    def _addr6(self, dom: Domain, t: float) -> str:
        h = _h(dom.name, self.version(dom, t), 6)
        return f"2001:db8:{h % 65536:x}:{(h >> 16) % 65536:x}::{1 + (h >> 32) % 65535:x}"

    def soa(self, name: str) -> d.Record:
        zone = name.rsplit(".", 1)[-1] if "." in name else name
        return d.Record(zone, d.SOA, d.IN, NEG_TTL,
                        d.make_rdata(d.SOA, (f"ns1.nic.{zone}", f"hostmaster.nic.{zone}",
                                             2026102601, 7200, 900, 1209600, ZONE_SOA_MIN)))

    def resolve(self, name: str, qtype: int, t: float) -> tuple[int, list[d.Record], list[d.Record]]:
        """Authoritative truth at time t: (rcode, answers, authority)."""
        key = name.lower().rstrip(".")
        dom = self.domains.get(key)
        if dom is None:
            return d.NXDOMAIN, [], [self.soa(key)]
        answers = []
        if dom.target:
            answers.append(d.Record(key, d.CNAME, d.IN, dom.ttl, d.encode_name(dom.target)))
            if qtype == d.CNAME:
                return d.NOERROR, answers, []
            rc, more, auth = self.resolve(dom.target, qtype, t)
            return rc, answers + more, auth
        if qtype == d.A:
            answers.append(d.Record(key, d.A, d.IN, dom.ttl, d.make_rdata(d.A, self._addr4(dom, t))))
        elif qtype == d.AAAA and dom.has_aaaa:
            answers.append(d.Record(key, d.AAAA, d.IN, dom.ttl, d.make_rdata(d.AAAA, self._addr6(dom, t))))
        elif qtype == d.MX and dom.has_mx:
            answers.append(d.Record(key, d.MX, d.IN, 3600, d.make_rdata(d.MX, (10, "mx1." + key))))
        elif qtype == d.TXT:
            answers.append(d.Record(key, d.TXT, d.IN, 3600, d.make_rdata(d.TXT, f"v=spf1 -all profile={dom.profile}")))
        if not answers:
            return d.NOERROR, [], [self.soa(key)]  # NODATA
        return d.NOERROR, answers, []

    def base_latency(self, name: str) -> float:
        dom = self.domains.get(name.lower().rstrip("."))
        return dom.latency_ms if dom else 20 + (_h(name) % 120)

    def latency(self, name: str, rnd: random.Random | None = None) -> float:
        dom = self.domains.get(name.lower().rstrip("."))
        base = dom.latency_ms if dom else 20 + (_h(name) % 120)
        jitter = (rnd or random).uniform(0.85, 1.25)
        return base * jitter

    def profile(self, name: str) -> str:
        dom = self.domains.get(name.lower().rstrip("."))
        return dom.profile if dom else "nxdomain"


def typo(name: str, rnd: random.Random) -> str:
    """A plausible non-existent name: a mistyped domain or a dead tracker."""
    if rnd.random() < 0.5:
        label, rest = name.split(".", 1) if "." in name else (name, "lab")
        i = rnd.randrange(len(label))
        return label[:i] + rnd.choice("qxzj") + label[i + 1:] + "." + rest
    return f"{rnd.choice(SYL)}{rnd.choice(SYL)}{rnd.randrange(100)}.trk.lab"
