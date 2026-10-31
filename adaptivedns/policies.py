"""
Cache replacement policies.

Every policy tracks *keys only*; the cache store owns the data. The store
calls:

    policy.access(key)      -> a cached key was read (hit)
    policy.admit(key)       -> a new key is being inserted; returns a list of
                               keys the policy wants evicted to make room
    policy.remove(key)      -> the store deleted a key (expiry, invalidation)
    policy.evict_one()      -> store is over its byte budget; pick one victim

Three policies are provided so they can be compared live:

* LRU  - Least Recently Used. Great for recency-heavy traffic, but a single
         scan of one-off URLs flushes the whole cache.
* LFU  - Least Frequently Used (with LRU tie-break). Keeps popular objects,
         but old popular objects can linger long after they go cold.
* ARC  - Adaptive Replacement Cache (Megiddo & Modha, IBM, FAST 2003).
         Splits the cache into a "seen once" list (T1) and a "seen twice or
         more" list (T2), remembers recently evicted keys in ghost lists
         (B1, B2), and continuously tunes the target size p of T1 based on
         which ghost list gets hit. It adapts between LRU-like and LFU-like
         behaviour on its own - this is the "adaptive" in adaptive caching.
"""
from __future__ import annotations

from collections import OrderedDict
import itertools


class Policy:
    name = "base"

    def __init__(self, capacity: int):
        self.capacity = max(1, int(capacity))

    def access(self, key):  # pragma: no cover - interface
        raise NotImplementedError

    def admit(self, key) -> list:  # pragma: no cover - interface
        raise NotImplementedError

    def remove(self, key):  # pragma: no cover - interface
        raise NotImplementedError

    def evict_one(self):  # pragma: no cover - interface
        raise NotImplementedError

    def keys(self) -> list:  # pragma: no cover - interface
        raise NotImplementedError

    def describe(self) -> dict:
        return {"name": self.name, "capacity": self.capacity}


class LRUPolicy(Policy):
    name = "LRU"

    def __init__(self, capacity):
        super().__init__(capacity)
        self._od: OrderedDict = OrderedDict()

    def access(self, key):
        if key in self._od:
            self._od.move_to_end(key)

    def admit(self, key):
        evicted = []
        if key in self._od:
            self._od.move_to_end(key)
            return evicted
        while len(self._od) >= self.capacity:
            evicted.append(self._od.popitem(last=False)[0])
        self._od[key] = True
        return evicted

    def remove(self, key):
        self._od.pop(key, None)

    def evict_one(self):
        if not self._od:
            return None
        return self._od.popitem(last=False)[0]

    def keys(self):
        return list(self._od.keys())


class LFUPolicy(Policy):
    name = "LFU"

    def __init__(self, capacity):
        super().__init__(capacity)
        self._freq: dict = {}
        self._tick: dict = {}
        self._clock = itertools.count()

    def access(self, key):
        if key in self._freq:
            self._freq[key] += 1
            self._tick[key] = next(self._clock)

    def _victim(self):
        # lowest frequency, then least recently used among those
        return min(self._freq, key=lambda k: (self._freq[k], self._tick[k]))

    def admit(self, key):
        evicted = []
        if key in self._freq:
            self.access(key)
            return evicted
        while len(self._freq) >= self.capacity:
            v = self._victim()
            self.remove(v)
            evicted.append(v)
        self._freq[key] = 1
        self._tick[key] = next(self._clock)
        return evicted

    def remove(self, key):
        self._freq.pop(key, None)
        self._tick.pop(key, None)

    def evict_one(self):
        if not self._freq:
            return None
        v = self._victim()
        self.remove(v)
        return v

    def keys(self):
        return sorted(self._freq, key=lambda k: self._tick[k])

    def frequency(self, key):
        return self._freq.get(key, 0)


class ARCPolicy(Policy):
    """Adaptive Replacement Cache.

    T1: resident, seen once recently        B1: ghosts evicted from T1
    T2: resident, seen at least twice       B2: ghosts evicted from T2
    p : adaptive target size for T1 (0..c)
    """

    name = "ARC"

    def __init__(self, capacity):
        super().__init__(capacity)
        self.t1: OrderedDict = OrderedDict()
        self.t2: OrderedDict = OrderedDict()
        self.b1: OrderedDict = OrderedDict()
        self.b2: OrderedDict = OrderedDict()
        self.p = 0.0
        self.ghost_hits = {"B1": 0, "B2": 0}

    # -- helpers -----------------------------------------------------------
    def _replace(self, key_in_b2: bool):
        """Evict one resident key into the matching ghost list."""
        if self.t1 and (len(self.t1) > self.p or (key_in_b2 and len(self.t1) == int(self.p))):
            old, _ = self.t1.popitem(last=False)
            self.b1[old] = True
            return old
        if self.t2:
            old, _ = self.t2.popitem(last=False)
            self.b2[old] = True
            return old
        if self.t1:
            old, _ = self.t1.popitem(last=False)
            self.b1[old] = True
            return old
        return None

    # -- interface ---------------------------------------------------------
    def access(self, key):
        # Case I: hit in T1 or T2 -> promote to MRU of T2
        if key in self.t1:
            del self.t1[key]
            self.t2[key] = True
        elif key in self.t2:
            self.t2.move_to_end(key)

    def admit(self, key):
        c = self.capacity
        evicted = []
        if key in self.t1 or key in self.t2:
            self.access(key)
            return evicted

        if key in self.b1:
            # Case II: ghost hit in B1 -> recency is undervalued, grow T1
            self.ghost_hits["B1"] += 1
            delta = max(len(self.b2) / max(len(self.b1), 1), 1)
            self.p = min(float(c), self.p + delta)
            v = self._replace(False)
            if v is not None:
                evicted.append(v)
            del self.b1[key]
            self.t2[key] = True
            return evicted

        if key in self.b2:
            # Case III: ghost hit in B2 -> frequency is undervalued, shrink T1
            self.ghost_hits["B2"] += 1
            delta = max(len(self.b1) / max(len(self.b2), 1), 1)
            self.p = max(0.0, self.p - delta)
            v = self._replace(True)
            if v is not None:
                evicted.append(v)
            del self.b2[key]
            self.t2[key] = True
            return evicted

        # Case IV: completely new key
        l1 = len(self.t1) + len(self.b1)
        l2 = len(self.t2) + len(self.b2)
        if l1 >= c:
            if len(self.t1) < c:
                self.b1.popitem(last=False)
                v = self._replace(False)
                if v is not None:
                    evicted.append(v)
            else:
                old, _ = self.t1.popitem(last=False)
                evicted.append(old)
        elif l1 + l2 >= c:
            if l1 + l2 >= 2 * c and self.b2:
                self.b2.popitem(last=False)
            if len(self.t1) + len(self.t2) >= c:
                v = self._replace(False)
                if v is not None:
                    evicted.append(v)
        self.t1[key] = True
        return evicted

    def remove(self, key):
        # explicit removal (expired/invalidated) - do not create a ghost
        self.t1.pop(key, None)
        self.t2.pop(key, None)

    def evict_one(self):
        return self._replace(False)

    def keys(self):
        return list(self.t1.keys()) + list(self.t2.keys())

    def segment(self, key):
        if key in self.t1:
            return "T1"
        if key in self.t2:
            return "T2"
        return None

    def describe(self):
        return {
            "name": self.name,
            "capacity": self.capacity,
            "p": round(self.p, 2),
            "t1": len(self.t1),
            "t2": len(self.t2),
            "b1": len(self.b1),
            "b2": len(self.b2),
            "ghost_hits": dict(self.ghost_hits),
        }


POLICIES = {"LRU": LRUPolicy, "LFU": LFUPolicy, "ARC": ARCPolicy}


def make_policy(name: str, capacity: int) -> Policy:
    try:
        return POLICIES[name.upper()](capacity)
    except KeyError:
        raise ValueError(f"unknown policy {name!r}; choose from {', '.join(POLICIES)}")
