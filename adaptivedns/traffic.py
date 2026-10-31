"""Live client workload: simulated users sending real DNS packets to the resolver."""
from __future__ import annotations

import asyncio
import bisect
import itertools
import random
import time

from . import dnsmsg as d
from .upstream import UpstreamClient, UpstreamError
from .zone import typo

REAL_DOMAINS = """google.com youtube.com facebook.com wikipedia.org instagram.com amazon.com x.com reddit.com
whatsapp.com linkedin.com netflix.com microsoft.com apple.com github.com stackoverflow.com bing.com
yahoo.com zoom.us office.com live.com twitch.tv spotify.com adobe.com cloudflare.com dropbox.com
paypal.com ebay.com imdb.com bbc.co.uk nytimes.com cnn.com medium.com quora.com pinterest.com
flipkart.com hotstar.com irctc.co.in sbi.co.in amazon.in zomato.com swiggy.com paytm.com
ndtv.com timesofindia.indiatimes.com python.org npmjs.com docker.com kaggle.com openai.com anthropic.com
canva.com notion.so slack.com discord.com telegram.org gstatic.com googleapis.com
wordpress.org mozilla.org w3schools.com geeksforgeeks.org leetcode.com coursera.org udemy.com khanacademy.org
nptel.ac.in swayam.gov.in india.gov.in uidai.gov.in incometax.gov.in rbi.org.in isro.gov.in iitb.ac.in iitm.ac.in
myntra.com ajio.com nykaa.com meesho.com bigbasket.com blinkit.com zeptonow.com bookmyshow.com makemytrip.com
olacabs.com uber.com phonepe.com hdfcbank.com icicibank.com axisbank.com kotak.com jio.com airtel.in
thehindu.com indianexpress.com hindustantimes.com livemint.com moneycontrol.com cricbuzz.com espncricinfo.com
duckduckgo.com tiktok.com snapchat.com tumblr.com vimeo.com soundcloud.com bandcamp.com archive.org
nasa.gov who.int un.org ietf.org rfc-editor.org iana.org icann.org arxiv.org nature.com sciencedirect.com
gitlab.com bitbucket.org atlassian.com figma.com trello.com asana.com salesforce.com oracle.com ibm.com intel.com
nvidia.com amd.com samsung.com sony.com dell.com hp.com lenovo.com""".split()


class TrafficGenerator:
    def __init__(self, resolver_addr, names, rate=20.0, nx_ratio=0.1, seed=None, burst_every=40.0):
        self.client = UpstreamClient([resolver_addr], timeout=3.0)
        self.names = list(names)
        self.rnd = random.Random(seed)
        self.rnd.shuffle(self.names)
        w = [1 / (k ** 0.95) for k in range(1, len(self.names) + 1)]
        self.cdf = list(itertools.accumulate(w))
        self.rate, self.nx_ratio, self.burst_every = rate, nx_ratio, burst_every
        self.typos = [typo(self.rnd.choice(self.names[:100]), self.rnd) for _ in range(40)]
        self.running = False
        self.ends_at = 0.0
        self.sent = self.errors = 0
        self._next_burst = 0.0

    def pick(self):
        if self.rnd.random() < self.nx_ratio:
            return self.rnd.choice(self.typos), d.A
        name = self.names[bisect.bisect_left(self.cdf, self.rnd.random() * self.cdf[-1])]
        return name, (d.A if self.rnd.random() < .78 else d.AAAA)

    async def _one(self, name, qt):
        try:
            await self.client.query(name, qt)
        except UpstreamError:
            self.errors += 1
        self.sent += 1

    async def run(self, seconds):
        self.running = True
        self.ends_at = time.time() + seconds
        self._next_burst = time.time() + 8
        tasks = set()
        try:
            while self.running and time.time() < self.ends_at:
                if time.time() >= self._next_burst:
                    # flash crowd: many clients ask for the same new name at once
                    name = self.names[self.rnd.randrange(len(self.names) // 2, len(self.names))]
                    for _ in range(12):
                        t = asyncio.ensure_future(self._one(name, d.A))
                        tasks.add(t)
                        t.add_done_callback(tasks.discard)
                    self._next_burst = time.time() + self.burst_every
                name, qt = self.pick()
                t = asyncio.ensure_future(self._one(name, qt))
                tasks.add(t)
                t.add_done_callback(tasks.discard)
                await asyncio.sleep(self.rnd.expovariate(self.rate))
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self.running = False

    def start(self, seconds=60):
        if not self.running:
            self.running = True
            self.ends_at = time.time() + seconds
            asyncio.ensure_future(self.run(seconds))

    def stop(self):
        self.running = False

    def status(self):
        return {"running": self.running, "sent": self.sent, "errors": self.errors,
                "remaining": max(0, round(self.ends_at - time.time())) if self.running else 0}
