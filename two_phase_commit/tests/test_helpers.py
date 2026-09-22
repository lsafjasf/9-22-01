"""Shared harness for the fault-injection test suite."""

from __future__ import annotations

import os
import tempfile
import unittest

from two_phase_commit import forensics
from two_phase_commit.coordinator import Coordinator
from two_phase_commit.participant import Participant
from two_phase_commit.sim import Simulator, SimNetwork
from two_phase_commit.storage import DurableLog

COORD = "coordinator"


class Harness:
    def __init__(self, testcase, n=3, seed=0, net=None, no_voters=()):
        self.tc = testcase
        self.datadir = tempfile.mkdtemp(prefix="tpc-test-")
        self.n = n
        self.pids = ["p%d" % i for i in range(n)]
        self.no_voters = set(no_voters)
        self.network = net or SimNetwork(seed=seed)
        self.effects = {pid: {"commit": set(), "abort": set()} for pid in self.pids}
        self.hooks = {}
        self.sim = Simulator(self.network, self.factory)
        self.addCleanup = testcase.addCleanup
        testcase.addCleanup(self._cleanup)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.datadir, ignore_errors=True)

    def log_path(self, node_id):
        return os.path.join(self.datadir, node_id + ".log")

    def set_hook(self, key, fn):
        self.hooks[key] = fn

    def factory(self, node_id):
        if node_id == COORD:
            return Coordinator(
                node_id,
                DurableLog(self.log_path(COORD)),
                self.pids,
                decision_hook=self.hooks.get("coord_decision"),
            )
        effects = self.effects[node_id]

        def vote_fn(txn):
            return node_id not in self.no_voters

        def commit_fn(txn):
            # In-memory "resource" is lost on kill, but the participant
            # guards it with its durable APPLY record; re-adding is the
            # same idempotent operation, never a double commit.
            effects["commit"].add(txn)

        def abort_fn(txn):
            effects["abort"].add(txn)

        return Participant(
            node_id,
            DurableLog(self.log_path(node_id)),
            coordinator_id=COORD,
            vote_fn=vote_fn,
            commit_fn=commit_fn,
            abort_fn=abort_fn,
            prepared_hook=self.hooks.get(("prepared", node_id)),
            applied_hook=self.hooks.get(("applied", node_id)),
        )

    def start(self):
        self.sim.boot(COORD)
        for pid in self.pids:
            self.sim.boot(pid)

    def begin(self, txn, at=0.1):
        self.sim.call(COORD, at, lambda node, t=txn: node.begin_txn(t))

    def kill(self, node_id, at):
        self.sim.kill_at(node_id, at)

    def boot(self, node_id, at):
        self.sim.boot_at(node_id, at=at)

    def partition(self, a, b, at):
        self.sim.schedule(at, ("partition", a, b))

    def heal(self, a, b, at):
        self.sim.schedule(at, ("heal", a, b))

    def run(self, budget=20.0):
        import heapq
        events = []
        while self.sim._events:
            events.append(heapq.heappop(self.sim._events))
        for at, seq, event in events:
            if event[0] == "partition":
                self.network.partition(event[1], event[2])
            elif event[0] == "heal":
                self.network.heal(event[1], event[2])
            else:
                heapq.heappush(self.sim._events, (at, seq, event))
        self.sim.run_until(self.sim.time + budget)

    # -- assertions -------------------------------------------------------

    def participant_view(self, pid):
        return forensics.inspect_node(
            pid, self.log_path(pid), "participant"
        )

    def coord_view(self):
        return forensics.inspect_node(
            COORD, self.log_path(COORD), "coordinator"
        )

    def outcome(self, txn):
        committed, aborted, blocked = [], [], []
        for pid in self.pids:
            view = self.participant_view(pid)
            if view.has("APPLY", txn=txn, outcome="COMMIT"):
                committed.append(pid)
            elif view.has("APPLY", txn=txn, outcome="ABORT"):
                aborted.append(pid)
            elif view.has("VOTE", txn=txn, vote="VOTE_COMMIT"):
                blocked.append(pid)
        return committed, aborted, blocked

    def coord_decision(self, txn):
        decs = self.coord_view().payloads("DECISION", txn=txn)
        return decs[-1]["decision"] if decs else None

    def assert_uniform(self, txn, expected=None):
        committed, aborted, blocked = self.outcome(txn)
        self.tc.assertFalse(
            committed and aborted,
            "DIVERGENCE: commit=%s abort=%s blocked=%s"
            % (committed, aborted, blocked),
        )
        self.tc.assertEqual(
            blocked, [], "txn %s left blocked: %s" % (txn, blocked)
        )
        if expected == "COMMIT":
            self.tc.assertEqual(
                sorted(committed), sorted(self.pids),
                "expected all commit, got %s" % (committed,),
            )
        elif expected == "ABORT":
            self.tc.assertEqual(
                sorted(aborted), sorted(self.pids),
                "expected all abort, got %s" % (aborted,),
            )
        return committed, aborted
