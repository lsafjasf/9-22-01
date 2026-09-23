"""Simulated message channel with virtual time.

Supports fault injection: delay, drop, duplicate, reorder (via random
delays), and network partitions. Deterministic for a given seed.
"""
import heapq
import random


class Faults:
    def __init__(self, drop=0.0, dup=0.0, min_delay=0.0, max_delay=0.0):
        self.drop = drop
        self.dup = dup
        self.min_delay = min_delay
        self.max_delay = max_delay
        # set of (src, dst) pairs; "*" acts as wildcard
        self.partitions = set()

    def blocked(self, src, dst):
        return (
            (src, dst) in self.partitions
            or (src, "*") in self.partitions
            or ("*", dst) in self.partitions
        )


class Sim:
    def __init__(self, seed=0, faults=None):
        self.rng = random.Random(seed)
        self.faults = faults or Faults()
        self.time = 0.0
        self.nodes = {}
        self._pq = []  # (deliver_at, seq, msg)
        self._seq = 0
        self.dropped = 0
        self.delivered = 0

    def register(self, node):
        self.nodes[node.name] = node

    def send(self, src, dst, msg):
        msg = dict(msg)
        msg["src"] = src
        msg["dst"] = dst
        if self.faults.blocked(src, dst):
            self.dropped += 1
            return
        if self.rng.random() < self.faults.drop:
            self.dropped += 1
            return
        copies = 2 if self.rng.random() < self.faults.dup else 1
        for _ in range(copies):
            delay = self.rng.uniform(self.faults.min_delay, self.faults.max_delay)
            self._seq += 1
            heapq.heappush(self._pq, (self.time + delay, self._seq, msg))

    def step(self):
        """Advance to the next event (message delivery or node timer).
        Returns False when no events remain."""
        candidates = []
        if self._pq:
            candidates.append(self._pq[0][0])
        for n in self.nodes.values():
            if n.alive and n.next_timer_at is not None:
                candidates.append(n.next_timer_at)
        if not candidates:
            return False
        t = min(candidates)
        if t > self.time:
            self.time = t
        while self._pq and self._pq[0][0] <= self.time:
            _, _, msg = heapq.heappop(self._pq)
            node = self.nodes.get(msg["dst"])
            if node is not None and node.alive:
                self.delivered += 1
                node.on_message(msg, self)
            else:
                self.dropped += 1
        for n in list(self.nodes.values()):
            if n.alive and n.next_timer_at is not None and n.next_timer_at <= self.time:
                n.on_timer(self)
        return True

    def run(self, max_time, max_steps=1_000_000):
        steps = 0
        while self.time < max_time and steps < max_steps:
            if not self.step():
                break
            steps += 1
