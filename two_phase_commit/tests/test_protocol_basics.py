"""Baseline state-machine behaviour without crashes."""

import unittest

from two_phase_commit.sim import SimNetwork
from two_phase_commit.tests.test_helpers import Harness


class ProtocolBasicsTest(unittest.TestCase):
    def test_happy_path_commits_everyone(self):
        h = Harness(self, n=3, seed=1,
                    net=SimNetwork(latency=0.01, jitter=0.02, seed=1))
        h.start()
        h.begin("T1", at=0.1)
        h.run()
        h.assert_uniform("T1", expected="COMMIT")
        self.assertEqual(h.coord_decision("T1"), "COMMIT")

    def test_one_no_vote_aborts_everyone(self):
        h = Harness(self, n=4, no_voters=("p2",), seed=2)
        h.start()
        h.begin("T1", at=0.1)
        h.run()
        h.assert_uniform("T1", expected="ABORT")
        self.assertEqual(h.coord_decision("T1"), "ABORT")

    def test_duplicated_and_reordered_messages_still_commit(self):
        h = Harness(self, n=3, seed=3,
                    net=SimNetwork(latency=0.01, jitter=0.02,
                                   duplicate=0.6, reorder=0.6, seed=3))
        h.start()
        h.begin("T1", at=0.1)
        h.run()
        h.assert_uniform("T1", expected="COMMIT")

    def test_idempotent_commit_applied_once_per_participant(self):
        h = Harness(self, n=3, seed=4,
                    net=SimNetwork(duplicate=0.8, reorder=0.5, seed=4))
        h.start()
        h.begin("T1", at=0.1)
        h.run()
        h.assert_uniform("T1", expected="COMMIT")
        # APPLY(COMMIT) is the idempotency key: exactly one per participant.
        for pid in h.pids:
            applies = h.participant_view(pid).payloads(
                "APPLY", txn="T1", outcome="COMMIT"
            )
            self.assertEqual(len(applies), 1, pid)
            self.assertIn("T1", h.effects[pid]["commit"])
            self.assertNotIn("T1", h.effects[pid]["abort"])

    def test_multiple_independent_transactions(self):
        h = Harness(self, n=3, seed=5,
                    net=SimNetwork(duplicate=0.2, reorder=0.3, seed=5))
        h.start()
        for i in range(5):
            h.begin("T%d" % i, at=0.1 + 0.2 * i)
        h.run()
        for i in range(5):
            h.assert_uniform("T%d" % i, expected="COMMIT")

    def test_prepare_timeout_aborts(self):
        h = Harness(self, n=3, seed=6,
                    net=SimNetwork(loss=0.0, seed=6))
        h.start()
        h.begin("T1", at=0.1)
        # Partition everyone off right after PREPARE would be sent so the
        # coordinator collects no votes and times out.
        for pid in h.pids:
            h.partition("coordinator", pid, 0.05)
        h.run(budget=5.0)
        self.assertEqual(h.coord_decision("T1"), "ABORT")
        for pid in h.pids:
            h.heal("coordinator", pid, 3.0)
        h.run(budget=10.0)
        h.assert_uniform("T1", expected="ABORT")


if __name__ == "__main__":
    unittest.main()
