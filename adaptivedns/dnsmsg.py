"""
DNS message wire format (RFC 1035), written from scratch on top of `struct`.

    +---------------------+
    |        Header       |  12 bytes: ID, flags, 4 section counts
    +---------------------+
    |       Question      |  QNAME QTYPE QCLASS
    +---------------------+
    |        Answer       |  resource records
    +---------------------+
    |      Authority      |  (SOA here for negative answers)
    +---------------------+
    |      Additional     |  (EDNS0 OPT pseudo-record lives here)
    +---------------------+

Names inside record data (CNAME, NS, MX, SOA...) are decoded through
compression pointers and stored uncompressed, so cached records can be
re-encoded into any new response safely.
"""
from __future__ import annotations

import ipaddress
import random
import struct
from dataclasses import dataclass, field

TYPES = {1: "A", 2: "NS", 5: "CNAME", 6: "SOA", 12: "PTR", 15: "MX", 16: "TXT",
         28: "AAAA", 33: "SRV", 41: "OPT", 65: "HTTPS", 255: "ANY"}
TYPE_BY_NAME = {v: k for k, v in TYPES.items()}
RCODES = {0: "NOERROR", 1: "FORMERR", 2: "SERVFAIL", 3: "NXDOMAIN", 4: "NOTIMP", 5: "REFUSED"}
A, NS, CNAME, SOA, PTR, MX, TXT, AAAA, SRV, OPT = 1, 2, 5, 6, 12, 15, 16, 28, 33, 41
NOERROR, FORMERR, SERVFAIL, NXDOMAIN, NOTIMP, REFUSED = 0, 1, 2, 3, 4, 5
IN = 1


class DNSError(ValueError):
    pass


def type_name(t: int) -> str:
    return TYPES.get(t, f"TYPE{t}")


def parse_type(s) -> int:
    if isinstance(s, int):
        return s
    s = str(s).upper()
    if s in TYPE_BY_NAME:
        return TYPE_BY_NAME[s]
    if s.startswith("TYPE") and s[4:].isdigit():
        return int(s[4:])
    raise DNSError(f"unknown record type {s}")


# --- names -----------------------------------------------------------------
def encode_name(name: str) -> bytes:
    name = name.rstrip(".")
    if not name:
        return b"\x00"
    out = bytearray()
    for label in name.split("."):
        b = label.encode("idna") if not label.isascii() else label.encode()
        if not 0 < len(b) < 64:
            raise DNSError(f"bad label in {name!r}")
        out.append(len(b))
        out += b
    out.append(0)
    if len(out) > 255:
        raise DNSError("name too long")
    return bytes(out)


def decode_name(buf: bytes, off: int) -> tuple[str, int]:
    labels, jumped, end, hops = [], False, off, 0
    while True:
        if off >= len(buf):
            raise DNSError("name runs past end of packet")
        ln = buf[off]
        if ln & 0xC0 == 0xC0:  # compression pointer
            if off + 1 >= len(buf):
                raise DNSError("truncated pointer")
            ptr = ((ln & 0x3F) << 8) | buf[off + 1]
            if not jumped:
                end = off + 2
            jumped, off = True, ptr
            hops += 1
            if hops > 64:
                raise DNSError("compression loop")
            continue
        if ln & 0xC0:
            raise DNSError("bad label type")
        off += 1
        if ln == 0:
            break
        labels.append(buf[off:off + ln].decode("ascii", "replace"))
        off += ln
    return ".".join(labels), (end if jumped else off)


# --- records ---------------------------------------------------------------
@dataclass
class Record:
    name: str
    rtype: int
    rclass: int
    ttl: int
    rdata: bytes  # canonical (uncompressed) wire form

    @property
    def text(self) -> str:
        return rdata_to_text(self.rtype, self.rdata)

    def with_ttl(self, ttl: int) -> "Record":
        return Record(self.name, self.rtype, self.rclass, max(0, int(ttl)), self.rdata)

    def to_dict(self):
        return {"name": self.name, "type": type_name(self.rtype), "ttl": self.ttl, "data": self.text}


def _canonical_rdata(buf: bytes, off: int, rtype: int, rdlen: int) -> bytes:
    """Return rdata with any embedded names decompressed."""
    raw = buf[off:off + rdlen]
    if rtype in (NS, CNAME, PTR):
        n, _ = decode_name(buf, off)
        return encode_name(n)
    if rtype == MX:
        n, _ = decode_name(buf, off + 2)
        return raw[:2] + encode_name(n)
    if rtype == SOA:
        m, o = decode_name(buf, off)
        r, o = decode_name(buf, o)
        return encode_name(m) + encode_name(r) + buf[o:o + 20]
    if rtype == SRV:
        n, _ = decode_name(buf, off + 6)
        return raw[:6] + encode_name(n)
    return raw


def rdata_to_text(rtype: int, rd: bytes) -> str:
    try:
        if rtype == A and len(rd) == 4:
            return str(ipaddress.IPv4Address(rd))
        if rtype == AAAA and len(rd) == 16:
            return str(ipaddress.IPv6Address(rd))
        if rtype in (NS, CNAME, PTR):
            return decode_name(rd, 0)[0] + "."
        if rtype == MX:
            return f"{struct.unpack('!H', rd[:2])[0]} {decode_name(rd, 2)[0]}."
        if rtype == SOA:
            m, o = decode_name(rd, 0)
            r, o = decode_name(rd, o)
            serial, refresh, retry, expire, minimum = struct.unpack("!5I", rd[o:o + 20])
            return f"{m}. {r}. {serial} {refresh} {retry} {expire} {minimum}"
        if rtype == TXT:
            parts, i = [], 0
            while i < len(rd):
                ln = rd[i]
                parts.append('"' + rd[i + 1:i + 1 + ln].decode("utf-8", "replace") + '"')
                i += 1 + ln
            return " ".join(parts)
        if rtype == SRV:
            pr, w, port = struct.unpack("!3H", rd[:6])
            return f"{pr} {w} {port} {decode_name(rd, 6)[0]}."
    except (DNSError, struct.error, ValueError):
        pass
    return "\\# " + rd.hex()


def make_rdata(rtype: int, value) -> bytes:
    """Build rdata from a friendly value (used by the simulated upstream)."""
    if rtype == A:
        return ipaddress.IPv4Address(value).packed
    if rtype == AAAA:
        return ipaddress.IPv6Address(value).packed
    if rtype in (NS, CNAME, PTR):
        return encode_name(value)
    if rtype == MX:
        pref, host = value
        return struct.pack("!H", pref) + encode_name(host)
    if rtype == TXT:
        b = value.encode()
        return bytes([len(b)]) + b
    if rtype == SOA:
        mname, rname, serial, refresh, retry, expire, minimum = value
        return encode_name(mname) + encode_name(rname) + struct.pack("!5I", serial, refresh, retry, expire, minimum)
    raise DNSError(f"cannot build rdata for {type_name(rtype)}")


def soa_minimum(rec: Record) -> int:
    try:
        _, o = decode_name(rec.rdata, 0)
        _, o = decode_name(rec.rdata, o)
        return struct.unpack("!I", rec.rdata[o + 16:o + 20])[0]
    except (DNSError, struct.error):
        return 60


# --- messages --------------------------------------------------------------
@dataclass
class Question:
    name: str
    qtype: int
    qclass: int = IN

    @property
    def key(self):
        return (self.name.lower().rstrip("."), self.qtype)


@dataclass
class Message:
    id: int = 0
    qr: bool = False
    opcode: int = 0
    aa: bool = False
    tc: bool = False
    rd: bool = True
    ra: bool = False
    ad: bool = False
    cd: bool = False
    rcode: int = 0
    questions: list[Question] = field(default_factory=list)
    answers: list[Record] = field(default_factory=list)
    authority: list[Record] = field(default_factory=list)
    additional: list[Record] = field(default_factory=list)
    edns_size: int | None = None   # None = client did not send EDNS0

    @property
    def flags(self) -> int:
        return ((self.qr << 15) | (self.opcode << 11) | (self.aa << 10) | (self.tc << 9) |
                (self.rd << 8) | (self.ra << 7) | (self.ad << 5) | (self.cd << 4) | (self.rcode & 0xF))

    def flag_names(self) -> list[str]:
        return [n for n, v in (("QR", self.qr), ("AA", self.aa), ("TC", self.tc), ("RD", self.rd),
                               ("RA", self.ra), ("AD", self.ad), ("CD", self.cd)) if v]

    def to_bytes(self, max_size: int | None = None) -> bytes:
        def section(recs):
            out = bytearray()
            for r in recs:
                out += encode_name(r.name) + struct.pack("!HHIH", r.rtype, r.rclass, r.ttl, len(r.rdata)) + r.rdata
            return bytes(out)

        qd = b"".join(encode_name(q.name) + struct.pack("!HH", q.qtype, q.qclass) for q in self.questions)
        an, ns, ar = section(self.answers), section(self.authority), section(self.additional)
        arcount = len(self.additional)
        if self.edns_size is not None:
            ar += b"\x00" + struct.pack("!HHIH", OPT, self.edns_size, 0, 0)
            arcount += 1
        body = qd + an + ns + ar
        hdr = struct.pack("!6H", self.id, self.flags, len(self.questions), len(self.answers),
                          len(self.authority), arcount)
        pkt = hdr + body
        if max_size and len(pkt) > max_size:
            # does not fit: send header + question with TC set, client retries over TCP
            self.tc = True
            hdr = struct.pack("!6H", self.id, self.flags, len(self.questions), 0, 0, 0)
            pkt = hdr + qd
        return pkt

    def summary(self) -> dict:
        return {
            "id": self.id, "flags": self.flag_names(), "opcode": self.opcode,
            "rcode": RCODES.get(self.rcode, str(self.rcode)),
            "question": [{"name": q.name, "type": type_name(q.qtype)} for q in self.questions],
            "answers": [r.to_dict() for r in self.answers],
            "authority": [r.to_dict() for r in self.authority],
            "edns": self.edns_size,
        }


def parse(buf: bytes) -> Message:
    if len(buf) < 12:
        raise DNSError("packet shorter than a header")
    mid, flags, qd, an, ns, ar = struct.unpack("!6H", buf[:12])
    m = Message(
        id=mid, qr=bool(flags & 0x8000), opcode=(flags >> 11) & 0xF, aa=bool(flags & 0x400),
        tc=bool(flags & 0x200), rd=bool(flags & 0x100), ra=bool(flags & 0x80),
        ad=bool(flags & 0x20), cd=bool(flags & 0x10), rcode=flags & 0xF,
    )
    off = 12
    for _ in range(qd):
        name, off = decode_name(buf, off)
        if off + 4 > len(buf):
            raise DNSError("truncated question")
        qt, qc = struct.unpack("!HH", buf[off:off + 4])
        off += 4
        m.questions.append(Question(name, qt, qc))
    for count, target in ((an, m.answers), (ns, m.authority), (ar, m.additional)):
        for _ in range(count):
            name, off = decode_name(buf, off)
            if off + 10 > len(buf):
                raise DNSError("truncated record")
            rt, rc, ttl, rdl = struct.unpack("!HHIH", buf[off:off + 10])
            off += 10
            if off + rdl > len(buf):
                raise DNSError("truncated rdata")
            if rt == OPT:
                m.edns_size = max(512, rc)
            else:
                target.append(Record(name, rt, rc, ttl, _canonical_rdata(buf, off, rt, rdl)))
            off += rdl
    return m


def make_query(name: str, qtype=A, *, qid=None, rd=True, edns: int | None = 1232) -> Message:
    return Message(id=qid if qid is not None else random.randrange(65536), rd=rd,
                   questions=[Question(name.rstrip("."), parse_type(qtype))], edns_size=edns)


def reply_to(query: Message, rcode=NOERROR) -> Message:
    return Message(id=query.id, qr=True, opcode=query.opcode, rd=query.rd, ra=True, cd=query.cd,
                   rcode=rcode, questions=list(query.questions),
                   edns_size=1232 if query.edns_size is not None else None)
