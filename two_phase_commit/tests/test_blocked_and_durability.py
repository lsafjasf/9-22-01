"""Blocking semantics, block-list queries, and durability ordering."""

import os
import unittest

from two_phase_commit.sim import SimNetwork
from two_phase_commit.storage import DurableLog, LogCorruption
from two_phase_commit.tests.test_helpers import Harness


class BlockedListTest(unittest.TestCase):
    def test_participants_remain_blocked_until_decision(self):
        h = Harness(self, n=3, seed=21,
                    net=SimNetwork(loss=0.0, seed=21))
        # Kill coordinator right after COMMIT fsync; do not restart yet.
        fired = {"v": False}

        def hook(txn, decision):
            if decision == "COMMIT" and not fired["v"]:
                fired["v"] = True
                h.sim.kill("coordinator")

        h.set_hook("coord_decision", hook)
        h.start()
        h.begin("T1", at=0.1)
        h.run(budget=3.0)
        self.assertTrue(fired["v"])
        # Reboot every participant: with the coordinator still dead they
        # must recover to PREPARED from their logs and remain blocked.
        nodes = []
        for pid in h.pids:
            h.sim.kill(pid)
        for pid in h.pids:
            nodes.append(h.sim.boot(pid))
        h.run(budget=3.0)
        for node, pid in zip(nodes, h.pids):
            self.assertEqual(node.state_of("T1"), "PREPARED", pid)
            self.assertIn("T1", node.blocked())
        committed, aborted, blocked = h.outcome("T1")
        self.assertEqual(committed, [])
        self.assertEqual(aborted, [])
        self.assertEqual(sorted(blocked), sorted(h.pids))

        # Restart coordinator: block list drains, outcome uniform commit.
        h.sim.boot("coordinator")
        h.run(budget=10.0)
        h.assert_uniform("T1", expected="COMMIT")
        for pid in h.pids:
            node = h.sim.nodes[pid]
            self.assertEqual(node.blocked(), [])


class DurabilityOrderTest(unittest.TestCase):
    def test_decision_offsets_before_any_send_effect(self):
        # Structural proof via the decision hook: at hook time the
        # DECISION frame is already readable on disk.
        h = Harness(self, n=2, seed=22)
        seen = {}

        def hook(txn, decision):
            view = h.coord_view()
            decs = view.payloads("DECISION", txn=txn)
            seen["decision_on_disk"] = bool(decs)
            h.sim.kill("coordinator")

        h.set_hook("coord_decision", hook)
        h.start()
        h.begin("T1", at=0.1)
        h.boot("coordinator", 1.5)
        h.run()
        self.assertTrue(seen["decision_on_disk"])
        h.assert_uniform("T1", expected="COMMIT")

    def test_vote_durable_before_coordinator_can_decide(self):
        # Kill a participant immediately after its vote fsync; after
        # restart its PREPARED state is recovered, never re-voted NO.
        h = Harness(self, n=3, seed=23,
                    net=SimNetwork(duplicate=0.4, reorder=0.4, seed=23))
        once = {"v": False}

        def prepared_hook(txn):
            if not once["v"]:
                once["v"] = True
                h.sim.kill("p0")

        h.set_hook(("prepared", "p0"), prepared_hook)
        h.start()
        h.begin("T1", at=0.1)
        h.boot("p0", 0.7)
        h.run()
        self.assertTrue(once["v"])
        votes = h.participant_view("p0").payloads("VOTE", txn="T1")
        self.assertTrue(all(v["vote"] == "VOTE_COMMIT" for v in votes))
        h.assert_uniform("T1", expected="COMMIT")


class StorageTest(unittest.TestCase):
    def test_round_trip_and_fsync_records(self):
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "x.log")
        log = DurableLog(path)
        log.append("BEGIN", {"txn": "T"})
        log.append("DECISION", {"txn": "T", "decision": "COMMIT"})
        records = log.read_all()
        self.assertEqual([r[1] for r in records], ["BEGIN", "DECISION"])
        log.close()

    def test_truncated_frame_is_reported_with_prefix(self):
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "x.log")
        log = DurableLog(path)
        log.append("BEGIN", {"txn": "T"})
        log.append("DECISION", {"txn": "T", "decision": "COMMIT"})
        log.close()
        from two_phase_commit.storage import corrupt_tail
        corrupt_tail(path, mode="truncate")
        prefix, corruption = DurableLog(path).read_prefix()
        self.assertIsNotNone(corruption)
        self.assertEqual([r[1] for r in prefix], ["BEGIN"])
        with self.assertRaises(LogCorruption):
            DurableLog(path).read_all()


if __name__ == "__main__":
    unittest.main()
