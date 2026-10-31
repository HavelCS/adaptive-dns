# AdaptiveDNS — a Popularity- and Stability-Aware DNS Resolution-Caching Algorithm

**Project 21:** *Design and Evaluation of a Proposed Lightweight DNS Resolution-Caching Algorithm to Reduce Latency in a Simulated Client–Server Network* (Module 7, Python Sockets)

A working caching DNS resolver built on raw UDP/TCP sockets, with:
- a proposed adaptive caching algorithm,
- a simulated network to test it in,
- a built-in evaluation that compares it against no cache and a standard cache,
- a live dashboard for the demo.

Pure Python standard library. **No installs.** Python 3.9+ on Windows, macOS or Linux.

```bash
python -m adaptivedns --demo --open        # or double-click run.bat / ./run.sh
```

| | |
|---|---|
| DNS resolver | `127.0.0.1:8053`, UDP and TCP |
| Dashboard | http://127.0.0.1:8081 |
| Simulated upstream (with `--demo`) | `127.0.0.1:9053` |

Without `--demo` it forwards to real resolvers (1.1.1.1, 8.8.8.8) and you can resolve real names:

```bash
python -m adaptivedns --open
nslookup -port=8053 wikipedia.org 127.0.0.1
```

---

## The demo (5 minutes, in this order)

Start with `python -m adaptivedns --demo --open`. Everything below happens in the dashboard.

**1. One lookup, explained (1 min).**
- Click the second example name under the search box and press **Resolve**. It takes ~100–180 ms. The timeline shows the query going Client → Resolver → Upstream, and the packet panel shows the actual response bytes, colour-coded into header, question and answer sections.
- Press **Resolve** again. It now takes ~0.3 ms, marked *Hit*, and the upstream is "not needed".
- Point out that the TTL in the answer counts down.
- Then resolve `gogle.lab` twice. The first answer is NXDOMAIN from upstream; the second is a *Negative hit* from the cache.

*Say:* "A DNS lookup normally costs a WAN round trip, tens to hundreds of milliseconds. A cache turns it into a sub-millisecond local lookup. My algorithm is about getting more lookups onto that fast path, safely."

**2. Live traffic (1 min).** Press **Start user traffic**. Simulated users browse with realistic popularity: a few sites get most visits, plus typos and occasional flash crowds where many users look up one name at once.
- The hit ratio climbs, and green bars (cache) replace orange bars (upstream).
- *Prefetched* (cyan) answers are popular names the resolver refreshed before they expired.
- The ARC panel rebalances its T1/T2 lists.
- "Answers up to date" checks every answer against the true data.

**3. The algorithm (1 min).** Scroll to **Algorithm features** and switch features off while traffic runs: **Basic cache** turns everything off, **Full algorithm** turns it all back on. Each card shows a live counter of what that feature is doing.

**4. The wow moment: an outage (1 min).** Flip **Upstream outage** on. The upstream stops answering.
- Names already cached keep being answered instantly in magenta (*Stale*, from serve-stale).
- Only names never seen before fail.
- Flip it off and everything recovers.

*Say:* "A standard cache fails the moment its records expire during an outage. Mine keeps the network usable."

**5. The proof (1 min).** Press **Run evaluation**. About ten seconds later you get:
- the comparison charts for no cache, a basic TTL cache, and the proposed cache, on an identical workload,
- the ablation table showing what each feature contributes.

This is your results slide, generated live.

---

## Full algorithm vs basic cache, side by side

With the resolver running (`python -m adaptivedns --demo --open`), open a **second terminal** in the project folder and run:

```bash
python -m adaptivedns.compare              # all five patterns, about 1 minute
python -m adaptivedns.compare typos scan   # only some patterns
python -m adaptivedns.compare --list       # print the domain names used
```

**With Google DNS instead of the simulated internet.** Start the resolver with a real upstream, then run the same script. It detects real mode by itself and uses real domains (google.com, wikipedia.org, irctc.co.in…):

```bash
python -m adaptivedns --upstream 8.8.8.8 --open     # terminal 1
python -m adaptivedns.compare                       # terminal 2
python -m adaptivedns.compare --list --real         # the real names used
```

In real mode the `hot` pattern is skipped, because real sites use timers of minutes rather than 5 seconds. The outage switch works with real servers too: it cuts the resolver off from 8.8.8.8. If your network blocks outside DNS servers, use your network's own DNS server address instead of 8.8.8.8 (find it with `ipconfig /all` on Windows).

Each pattern runs twice, first with the full algorithm and then with a basic cache, on an empty cache each time:

| Pattern | Domains | Shows | Full algorithm | Basic cache |
|---|---|---|---|---|
| `typos` | gogle.lab, yotube.lab, faceboook.test, 4 times each | negative caching | 9 of 12 from memory | 0 of 12 |
| `crowd` | docsxatek.test, 25 users at once | coalescing | 1 query upstream | 25 queries |
| `scan` | 8 favourites twice, 60 one-off names, favourites again (cache of 20) | ARC | 8 of 8 favourites kept | 0 of 8 |
| `hot` | docszentek.test, datamar.test (5 s timer), every 0.5 s | prefetch | 2 users waited | 6 users waited |
| `outage` | 5 names with 5 s timers, then upstream down | serve-stale | 5 of 5 answered | 5 of 5 failed |

---

## The proposed algorithm

A standard caching resolver keeps each answer for exactly its TTL and evicts with LRU. The proposed algorithm adds six mechanisms. All are implemented in `cache.py` and `resolver.py`, and each can be switched off for the ablation study.

| # | Mechanism | Idea | Basis |
|---|---|---|---|
| 1 | **ARC replacement** | Two lists: names seen once (T1) and names seen again (T2), plus "ghost" lists of recently evicted names. When a ghost is requested again, the cache learns it evicted the wrong kind of name and moves the T1/T2 boundary `p`. Bursts of one-off lookups (typos, trackers) can't flush popular names. | Megiddo & Modha, FAST 2003 |
| 2 | **Popularity-aware prefetch** | Each entry has a popularity score, a hit count that decays exponentially: `score = score·e^(−Δt/τ) + 1`, with τ = 120 s. When a *popular* entry (score ≥ 3) enters the last 15% of its TTL, it is refreshed in the background. Unpopular entries just expire, so prefetching stays cheap. | Proposed |
| 3 | **Stability-learned TTL** | For every name, the resolver tracks how long its answer actually stays the same (its observed lifetime). It may cache an answer longer than the TTL, but never beyond **10% of the observed lifetime** and never beyond 4× the TTL. Names whose owners set tiny TTLs on records that never change get cached longer. Names that really change are not stretched. | Proposed |
| 4 | **Negative caching** | NXDOMAIN and "no such record" answers are cached for the SOA minimum. | RFC 2308 |
| 5 | **Serve-stale + fast fail** | If every upstream fails, expired answers are served with TTL 30 instead of SERVFAIL. Once the upstream is known to be down, stale answers are sent immediately rather than after another timeout, and the upstream is probed again in the background. | RFC 8767 |
| 6 | **Query coalescing** | When many clients ask for the same uncached name at once, one upstream query is shared. | Proposed for flash crowds |

**What's novel:** mechanisms 2 and 3. Prefetching driven by *decayed popularity*, and TTL extension bounded by *measured answer lifetime*, combined with ARC eviction in one resolver.

---

## Results

`python -m adaptivedns.experiment` replays the same 30-minute workload through each configuration:
- 54,733 queries at 30/s over 1,726 names,
- Zipf popularity that drifts every 10 minutes,
- 12% non-existent names,
- a flash crowd every 2 minutes,
- a 3-minute upstream outage,
- a 300-entry cache.

| Configuration | Mean latency | p95 | Hit ratio | Upstream queries | Outdated (of cached answers) | Answered during outage |
|---|---:|---:|---:|---:|---:|---:|
| No cache | 68.3 ms | 157 ms | 0% | 54,733 | – | 0% |
| Basic TTL cache (LRU) | 44.2 ms | 149 ms | 40.7% | 34,243 | 8.0% | 8% |
| **Proposed adaptive cache** | **35.2 ms** | **145 ms** | **54.0%** | **31,076** | **6.6%** | **36%** |

Compared with the basic cache, the proposed cache:
- cuts mean latency by **20%** (48% vs no cache),
- raises the hit ratio from 41% to **54%**,
- sends **9% fewer** queries upstream (43% fewer than no cache),
- answers **4.5× more** queries during the outage,
- serves *fewer* outdated answers.

### Ablation: what each feature contributes

| Proposed without… | Mean latency | Hit ratio | Upstream queries | Outdated | Outage |
|---|---:|---:|---:|---:|---:|
| (nothing removed) | 35.2 ms | 54.0% | 31,076 | 6.6% | 36% |
| ARC (→ LRU) | 39.7 ms | 48.4% | 30,472 | 5.4% | 30% |
| Prefetch | 38.3 ms | 49.8% | 28,235 | 7.0% | 36% |
| TTL stretching | 35.5 ms | 53.7% | 33,080 | 6.6% | 36% |
| Negative caching | 39.7 ms | 46.8% | 34,832 | 7.9% | 32% |
| Serve-stale | 35.8 ms | 53.4% | 31,288 | 6.3% | 16% |
| Coalescing | 35.3 ms | 53.8% | 31,161 | 6.6% | 36% |

**How to read it (and what to say in the viva):**
- **ARC** and **negative caching** give the biggest latency gains.
- **Prefetch** is a deliberate trade: it spends about 2,800 extra upstream queries to lift the hit ratio by 4 points and cut latency by 3 ms. Because it only refreshes *popular* names, the cost stays low.
- **TTL stretching** is mainly about upstream load: it cuts upstream queries by about 6% with **no measurable accuracy cost** (6.6% outdated with or without it). That is what the 10%-of-lifetime safety bound is for.
- **Serve-stale** more than doubles the queries answered during the outage.
- **Coalescing** barely moves averages. It matters during flash crowds, where it stops dozens of identical queries hitting the upstream at once.
- Why ARC's removal *lowers* "outdated": ARC keeps more entries cached, so more answers come from cache. Any cached answer can go out of date before its TTL ends. This is inherent to DNS and why TTLs exist.

"Outdated" counts cached answers that no longer matched the authoritative data at that moment. Even the basic cache has 8% because records can change before their TTL expires.

---

## Networking concepts demonstrated

- **DNS wire format (RFC 1035)**, implemented by hand:
  - header flags and section counts,
  - label encoding, and name compression pointers (with loop protection),
  - resource records: A, AAAA, CNAME, NS, MX, TXT, SOA, SRV.
- **UDP and TCP transport.** The 512-byte UDP limit, EDNS0 for larger UDP payloads, the TC (truncated) bit, and retry over TCP with 2-byte length framing.
- **Caching semantics.** TTLs counted down in served answers, negative caching (RFC 2308), serve-stale (RFC 8767), CNAME chains.
- **Cache poisoning defences.** A random 16-bit transaction ID and a random source port per query, and replies accepted only if the source, ID and question all match.
- **Reliability.** Timeouts, failover across multiple upstreams, fast-fail when the upstream is down.
- **Asynchronous I/O.** A single event loop (`asyncio`) serves thousands of concurrent queries.
- **Workload modelling.** Zipf popularity, Poisson arrivals, flash crowds.

---

## Architecture

```
  nslookup / dig / OS          ┌─────────────────── AdaptiveDNS (asyncio) ───────────────────┐
  dashboard / load gen  ─UDP─▶ │ resolver.py   parse ─▶ block list ─▶ cache lookup            │ ─UDP─▶ upstream
                        ◀─────  │                         fresh → HIT / PREFETCHED / NEG_HIT   │ ◀────  (real or simulated)
                         TCP    │                         in flight → COALESCED                │
                                │                         upstream ok → MISS (store)           │
                                │                         upstream down → STALE / SERVFAIL     │
                                │ cache.py      ARC · popularity · lifetime learner · negative │
                                │ background    prefetch popular entries · purge               │
                                │ dashboard.py  HTTP + Server-Sent Events ─▶ browser UI        │
                                └──────────────────────────────────────────────────────────────┘
```

```
adaptive-dns/
├── adaptivedns/
│   ├── __main__.py      start resolver + dashboard (+ simulated upstream with --demo)
│   ├── dnsmsg.py        DNS packet encoder/decoder (RFC 1035) from scratch
│   ├── resolver.py      UDP/TCP DNS server and the resolution logic
│   ├── cache.py         the proposed adaptive cache
│   ├── policies.py      LRU, LFU, ARC replacement policies
│   ├── upstream.py      DNS client: random ID/port, validation, failover, TCP fallback
│   ├── zone.py          synthetic Internet: 1,700+ names with realistic TTL behaviour
│   ├── simupstream.py   simulated upstream resolver with WAN delays and outage switch
│   ├── experiment.py    evaluation: identical workload through each config, virtual time
│   ├── traffic.py       live simulated users
│   ├── query.py         a small dig-like client
│   ├── stats.py         metrics
│   ├── dashboard.py     dashboard server
│   └── static/          dashboard UI (HTML/CSS/JS, no frameworks)
├── tests/test_adaptivedns.py
└── README.md
```

### The simulated network (`zone.py`)

Each name follows a profile modelled on real DNS:

| Profile | TTL | Real change rate | Represents |
|---|---|---|---|
| stable | 300 s | never | ordinary websites |
| cdn | 20 s | every 3 min | CDNs that publish low TTLs "just in case" |
| volatile | 60 s | every 45 s | failover / dynamic DNS |
| alias | 120 s | CNAME → CDN edge | `www` names behind a CDN |
| short | 5 s | never | over-cautious tiny TTLs |

Each name also has its own simulated recursive-resolution delay: most are 20–60 ms, with a long tail up to ~180 ms.

### Why the evaluation runs in virtual time

TTLs are seconds to minutes, so a fair comparison needs tens of minutes of traffic per configuration. The evaluation runs the *real* `DNSCache` code against a virtual clock. That replays 30 minutes in about a second per configuration, deterministically, so the results are reproducible. The live demo uses the same cache in real time over real sockets.

---

## Command line

```
python -m adaptivedns [--demo] [--open] [--port 8053] [--ui-port 8081]
                      [--upstream IP[:PORT] ...] [--max-entries 300] [--basic] [--block DOMAIN ...]

python -m adaptivedns.experiment [--minutes 30] [--qps 30] [--capacity 300] [--json out.json]
python -m adaptivedns.query NAME [TYPE] [--server 127.0.0.1:8053]
python -m unittest discover -s tests -v        # 23 tests
```

To use it as your computer's real DNS server, run it on port 53. That needs admin rights: `sudo python -m adaptivedns --port 53`, or an Administrator prompt on Windows. Then set your network adapter's DNS server to 127.0.0.1.

---

## Likely viva questions

**Isn't serving stale or stretched answers wrong?** Serve-stale is an IETF standard (RFC 8767) and is only used when the upstream is unreachable, where the alternative is no answer at all. Stretching is bounded to 10% of how long that name's answer has actually been observed to last. The evaluation measures the cost: outdated answers do not increase (6.6% with or without stretching).

**Why ARC instead of LRU?** DNS traffic mixes a stable set of popular names with a flood of one-off lookups (typos, trackers, unique subdomains). LRU lets every one-off push out a popular name. ARC keeps names seen twice separately, and tunes itself using ghost hits. The ablation shows removing it costs 4.5 ms and 5.6 points of hit ratio.

**Doesn't prefetching add load?** Yes, about 2,800 queries over 30 minutes. It only refreshes names with high *recent* popularity, so it buys a 4-point hit-ratio gain and 3 ms lower latency. The upstream count in the table includes prefetches, so the cost is not hidden.

**How do you prevent cache poisoning?** Each upstream query uses a random 16-bit ID and a fresh random source port. A reply is accepted only if it comes from the server we asked, carries our ID, and echoes our exact question. An attacker would have to guess about 2³² combinations within the response window.

**What happens with large responses?** Over UDP without EDNS0, responses are limited to 512 bytes. Larger ones are sent with the TC bit set and the client retries over TCP, which the resolver also serves. The upstream client does the same in reverse.

**What would you do next?**
- DNSSEC validation.
- DNS-over-HTTPS/TLS upstream.
- Persisting the learned lifetimes across restarts.
- Learning per-domain popularity cycles (daily patterns) for time-of-day prefetching.

## References

- RFC 1034/1035 — Domain Names; RFC 2308 — Negative Caching; RFC 6891 — EDNS(0); RFC 8767 — Serving Stale Data
- N. Megiddo, D. Modha, "ARC: A Self-Tuning, Low Overhead Replacement Cache", USENIX FAST 2003
- J. Jung et al., "DNS Performance and the Effectiveness of Caching", IEEE/ACM ToN 2002
- L. Breslau et al., "Web Caching and Zipf-like Distributions", IEEE INFOCOM 1999
