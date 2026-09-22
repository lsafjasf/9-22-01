"""Crashes, restarts, partitions and combined fault scenarios."""

import unittest

from two_phase_commit.sim import SimNetwork
from two_phase_commit.tests.test_helpers import Harness


class CrashRecoveryTest(unittest.TestCase):
    def test_kill_coordinator_right_after_commit_fsync_completes_commit(self):
        # The decisive ordering test: the coordinator is killed strictly
        # between the COMMIT fsync and the first COMMIT send.
        h = Harness(self, n=3, seed=11)
        state = {"killed": False}

        def hook(txn, decision):
            if decision == "COMMIT" and not state["killed"]:
                state["killed"] = True
                h.sim.kill("coordinator")

        h.set_hook("coord_decision", hook)
        h.start()
        h.begin("T1", at=0.1)
        h.boot("coordinator", 1.5)
        h.run()
        self.assertTrue(state["killed"])
        h.assert_uniform("T1", expected="COMMIT")

    def test_kill_coordinator_before_decision_presumes_abort(self):
        h = Harness(self, n=3, seed=12,
                    net=SimNetwork(loss=0.1, duplicate=0.1, seed=12))
        h.start()
        h.begin("T1", at=0.1)
        h.kill("coordinator", 0.6)   # during WAIT, before any decision
        h.boot("coordinator", 1.6)
        h.run()
        h.assert_uniform("T1", expected="ABORT")

    def test_kill_participant_after_vote_before_commit(self):
        h = Harness(self, n=3, seed=13,
                    net=SimNetwork(reorder=0.3, duplicate=0.3, seed=13))
        h.set_hook(("prepared", "p1"), lambda txn: h.sim.kill("p1"))
        h.start()
        h.begin("T1", at=0.1)
        h.boot("p1", 0.7)
        h.run()
        h.assert_uniform("T1", expected="COMMIT")
        # The vote record survived and there is one durable APPLY.
        applies = h.participant_view("p1").payloads(
            "APPLY", txn="T1", outcome="COMMIT"
        )
        self.assertEqual(len(applies), 1)

    def test_kill_participant_after_apply_fsync_before_ack(self):
        h = Harness(self, n=3, seed=14,
                    net=SimNetwork(duplicate=0.5, seed=14))

        def applied(txn, outcome):
            if outcome == "COMMIT":
                h.sim.kill("p2")

        h.set_hook(("applied", "p2"), applied)
        h.start()
        h.begin("T1", at=0.1)
        h.boot("p2", 2.0)
        h.run()
        h.assert_uniform("T1", expected="COMMIT")
        # Effect must not have been replayed after restart.
        self.assertEqual(
            len(h.participant_view("p2").payloads(
                "APPLY", txn="T1", outcome="COMMIT")),
            1,
        )

    def test_partition_mid_phase2_heals_and_converges(self):
        h = Harness(self, n=3, seed=15,
                    net=SimNetwork(loss=0.1, duplicate=0.2, seed=15))
        h.start()
        h.begin("T1", at=0.1)
        h.partition("coordinator", "p2", 0.9)
        h.heal("coordinator", "p2", 3.0)
        h.run(budget=12.0)
        h.assert_uniform("T1", expected="COMMIT")

    def test_repeated_coordinator_kills_across_both_phases(self):
        h = Harness(self, n=3, seed=16,
                    net=SimNetwork(loss=0.1, duplicate=0.15,
                                   reorder=0.2, seed=16))
        kills = {"n": 0}

        def hook(txn, decision):
            if kills["n"] < 3:
                kills["n"] += 1
                h.sim.kill("coordinator")

        h.set_hook("coord_decision", hook)
        h.start()
        h.begin("T1", at=0.1)
        h.kill("coordinator", 0.55)
        h.boot("coordinator", 1.2)
        h.boot("coordinator", 2.2)   # boot is idempotent if alive
        h.kill("coordinator", 3.0)
        h.boot("coordinator", 3.8)
        h.run(budget=15.0)
        self.assertGreaterEqual(kills["n"], 1)
        h.assert_uniform("T1", expected="COMMIT")

    def test_all_participants_crash_and_restart(self):
        h = Harness(self, n=4, seed=17,
                    net=SimNetwork(duplicate=0.2, reorder=0.2, seed=17))
        h.start()
        h.begin("T1", at=0.1)
        for i, pid in enumerate(h.pids):
            h.kill(pid, 0.8 + 0.3 * i)
            h.boot(pid, 2.5 + 0.3 * i)
        h.run(budget=15.0)
        h.assert_uniform("T1", expected="COMMIT")

    def test_no_voter_with_participant_crashes_aborts_uniformly(self):
        h = Harness(self, n=3, seed=18, no_voters=("p0",),
                    net=SimNetwork(loss=0.1, duplicate=0.2, seed=18))
        h.start()
        h.begin("T1", at=0.1)
        h.kill("p1", 0.9)
        h.boot("p1", 2.8)
        h.kill("coordinator", 1.2)
        h.boot("coordinator", 2.0)
        h.run(budget=15.0)
        h.assert_uniform("T1", expected="ABORT")


if __name__ == "__main__":
    unittest.main()
