"""Deterministic simulated runtime for tests and demos.

Real clocks and sockets are deliberately avoided so that every fault
injection test is reproducible from a random seed.  The simulator is a
discrete event loop:

* every node has a logical inbox;
* :class:`SimNetwork` applies latency / loss / duplication / reordering /
  partitions when a message is *sent*, scheduling the receive event;
* ``node.set_timer(seconds, token)`` schedules a timeout;
* ``sim.kill(node_id)`` loses all in-memory state and pending events;
* ``sim.boot(node_id)`` re-creates the node, which must recover purely
  from its durable log.

The same :class:`Context` interface is the only thing the coordinator and
participant use, so they could be re-hosted on a real transport later by
implementing ``send`` / ``set_timer`` against sockets and real timers.
"""

from __future__ import annotations

import heapq
import random

from .protocol import Msg


class NetworkStats:
    def __init__(self):
        self.sent = 0
        self.delivered = 0
        self.dropped = 0
        self.duplicated = 0
        self.reordered = 0
        self.offline_drops = 0

    def as_dict(self):
        return dict(self.__dict__)


class SimNetwork:
    """Fault-injecting in-memory channel.

    Fault knobs (all overridable per send via ``rng``):

    * ``latency``     base delay in simulated seconds
    * ``jitter``      uniform extra delay in [0, jitter)
    * ``loss``        probability a message is permanently dropped
    * ``duplicate``   probability an extra copy is scheduled
    * ``reorder``     probability a message is deliberately delayed so it
                      overtakes / is overtaken by later messages
    """

    def __init__(
        self,
        latency=0.01,
        jitter=0.02,
        loss=0.0,
        duplicate=0.0,
        reorder=0.0,
        seed=0,
    ):
        self.latency = latency
        self.jitter = jitter
        self.loss = loss
        self.duplicate = duplicate
        self.reorder = reorder
        self.rng = random.Random(seed)
        self.stats = NetworkStats()
        # Undirected partitions: frozen sets of node id pairs that cannot
        # exchange messages right now.
        self._partitions = set()

    def partition(self, a, b):
        self._partitions.add(frozenset((a, b)))

    def heal(self, a=None, b=None):
        if a is None and b is None:
            self._partitions.clear()
        else:
            self._partitions.discard(frozenset((a, b)))

    def _blocked(self, a, b):
        return frozenset((a, b)) in self._partitions

    def _one_delay(self, reordered):
        delay = self.latency + self.rng.random() * self.jitter
        if reordered:
            # Push this message well past the normal window so later,
            # un-delayed messages arrive first.
            delay += 2 * (self.latency + self.jitter)
        return delay

    def send(self, sim, msg):
        """Apply faults and enqueue receive events."""

        self.stats.sent += 1
        alive = sim.alive
        if msg.src not in alive or msg.dst not in alive:
            self.stats.offline_drops += 1
            return
        if self._blocked(msg.src, msg.dst):
            self.stats.dropped += 1
            return
        if self.rng.random() < self.loss:
            self.stats.dropped += 1
            return

        reordered = self.rng.random() < self.reorder
        sim.schedule_after(self._one_delay(reordered), ("deliver", msg))
        if reordered:
            self.stats.reordered += 1
        self.stats.delivered += 1

        if self.rng.random() < self.duplicate:
            dup = Msg(msg.kind, msg.txn, msg.src, msg.dst)
            sim.schedule_after(self._one_delay(False), ("deliver", dup))
            self.stats.duplicated += 1
            self.stats.delivered += 1


class Context:
    """The I/O surface handed to a node (transport + timers + time)."""

    def __init__(self, sim, node_id):
        self._sim = sim
        self.node_id = node_id

    @property
    def time(self):
        return self._sim.time

    def send(self, msg):
        if msg.src is None:
            msg.src = self.node_id
        if msg.dst is None:
            raise ValueError("message requires a destination")
        self._sim.network.send(self._sim, msg)

    def set_timer(self, delay, token):
        return self._sim._new_timer(self.node_id, delay, token)

    def cancel_timer(self, handle):
        self._sim._cancel_timer(handle)

class Simulator:
    """Discrete-event engine.  ``factory(node_id)`` builds a fresh node."""

    def __init__(self, network, factory, log_factory=None):
        self.network = network
        self._factory = factory
        self.log_factory = log_factory
        self.time = 0.0
        self._events = []          # (time, seq, event)
        self._seq = 0
        self._alive = set()
        self.nodes = {}            # node_id -> live node instance
        self._timers = {}          # handle -> (time, node_id, token)
        self._timer_seq = 0
        self.pre_crash_hooks = {}  # node_id -> callable(node)
        self.trace = []            # list of (time, string)

    # -- node lifecycle ---------------------------------------------------

    @property
    def alive(self):
        return set(self._alive)

    def node_factory(self, node_id):
        return self._factory(node_id)

    def boot(self, node_id, now=None):
        if node_id in self._alive:
            return
        ctx = Context(self, node_id)
        node = self._factory(node_id)
        self.nodes[node_id] = node
        self._alive.add(node_id)
        node.boot(ctx)
        self.trace.append((self.time, "boot %s" % node_id))
        return node

    def kill(self, node_id):
        """Hard kill: drop the instance, volatile state and pending events."""

        if node_id not in self._alive:
            return
        node = self.nodes.get(node_id)
        hook = self.pre_crash_hooks.get(node_id)
        if hook is not None and node is not None:
            hook(node)
        self._alive.discard(node_id)
        self.nodes.pop(node_id, None)
        # Drop all pending events owned by this node (its timers).
        self._timers = {
            h: ev for h, ev in self._timers.items() if ev[1] != node_id
        }
        self.trace.append((self.time, "kill %s" % node_id))

    # -- events -----------------------------------------------------------

    def schedule(self, at, event):
        heapq.heappush(self._events, (at, self._seq, event))
        self._seq += 1

    def schedule_after(self, delay, event):
        self.schedule(self.time + max(0.0, delay), event)

    def _new_timer(self, node_id, delay, token):
        self._timer_seq += 1
        handle = self._timer_seq
        self.schedule_after(delay, ("timer", node_id, handle, token))
        self._timers[handle] = (self.time + delay, node_id, token)
        return handle

    def _cancel_timer(self, handle):
        self._timers.pop(handle, None)

    def run(self, until=None, max_events=1_000_000):
        """Process events; returns the number processed."""

        processed = 0
        while self._events and processed < max_events:
            at, _s, event = self._events[0]
            if until is not None and at > until:
                return processed
            heapq.heappop(self._events)
            self.time = at
            kind = event[0]
            if kind == "deliver":
                msg = event[1]
                if msg.src in self._alive and msg.dst in self._alive:
                    node = self.nodes.get(msg.dst)
                    if node is not None:
                        node.handle(msg)
            elif kind == "timer":
                _, node_id, handle, token = event
                if handle not in self._timers:
                    continue
                if node_id not in self._alive:
                    continue
                self._timers.pop(handle, None)
                node = self.nodes.get(node_id)
                if node is not None:
                    node.on_timer(token)
            elif kind == "call":
                _, node_id, fn, args, kwargs = event
                if node_id in self._alive:
                    fn(self.nodes[node_id], *args, **kwargs)
            elif kind == "boot-node":
                self.boot(event[1])
            elif kind == "kill-node":
                self.kill(event[1])
            else:
                raise ValueError("unknown event %r" % (event,))
            processed += 1
        return processed

    def call(self, node_id, delay, fn, *args, **kwargs):
        """Schedule an external action on a live node (test/demo use)."""

        self.schedule_after(delay, ("call", node_id, fn, args, kwargs))

    def boot_at(self, node_id, at=None, delay=None):
        """Schedule a (re)boot; valid even if the node is currently dead."""

        if at is not None:
            self.schedule(at, ("boot-node", node_id))
        else:
            self.schedule_after(delay or 0.0, ("boot-node", node_id))

    def kill_at(self, node_id, at):
        self.schedule(at, ("kill-node", node_id))

    def run_until(self, deadline):
        """Process every event scheduled at or before ``deadline``."""

        self.run(until=deadline, max_events=10_000_000)
        return self.time

    def run_until_quiescent(self, idle=0.5, budget=200.0):
        """Process events until an idle gap of ``idle`` or the budget."""

        deadline = self.time + budget
        while self._events and self.time < deadline:
            nxt = self._events[0][0]
            if nxt > deadline:
                break
            if nxt - self.time > idle:
                # Genuine idle gap: nothing pending within the window.
                break
            self.run(until=nxt + 1e-12, max_events=10_000_000)
        return self.time
