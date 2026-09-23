import os
import random
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tpc import Coordinator, Participant, Sim, Faults, LogCorruptError
from tpc.doctor import analyze

COORD = "coord"
PARTS = ["p1", "p2", "p3"]


def make_cluster(dir, seed=0, faults=None, votes=None, vote_timeout=5.0):
    sim = Sim(seed=seed, faults=faults or Faults())
    coord = Coordinator(COORD, dir, vote_timeout=vote_timeout)
    votes = votes or {}
    parts = {p: Participant(p, dir, coordinator=COORD,
                            vote=votes.get(p, "yes")) for p in PARTS}
    coord.start(sim)
    for p in parts.values():
        p.start(sim)
    return sim, coord, parts


def decisions(parts, txn="T"):
    return {p.name: p.decision_of(txn) for p in parts.values()}


def assert_consistent(testcase, coord, parts, txn="T"):
    dec = [d for d in decisions(parts, txn).values() if d is not None]
    testcase.assertLessEqual(len(set(dec)), 1,
                             "inconsistent participant decisions: %s" % dec)
    cdec = coord.txns[txn].decision if txn in coord.txns else None
    if cdec and dec:
        testcase.assertEqual(set(dec), {cdec})


class TestBasic(unittest.TestCase):
    def test_commit_happy_path(self):
        with tempfile.TemporaryDirectory() as d:
            sim, coord, parts = make_cluster(d)
            coord.begin(sim, "T", PARTS)
            sim.run(50)
            self.assertEqual(coord.txns["T"].decision, "commit")
            self.assertTrue(coord.txns["T"].done)
            self.assertEqual(set(decisions(parts, "T").values()), {"commit"})
            for p in parts.values():
                self.assertEqual(p.blocked(), {})

    def test_vote_no_aborts_everywhere(self):
        with tempfile.TemporaryDirectory() as d:
            sim, coord, parts = make_cluster(d, votes={"p2": "no"})
            coord.begin(sim, "T", PARTS)
            sim.run(50)
            self.assertEqual(coord.txns["T"].decision, "abort")
            self.assertEqual(set(decisions(parts, "T").values()), {"abort"})

    def test_participant_crash_before_prepare_aborts(self):
        with tempfile.TemporaryDirectory() as d:
            sim, coord, parts = make_cluster(d)
            parts["p3"].crash()
            coord.begin(sim, "T", PARTS)
            sim.run(10)  # vote timeout at t=5
            self.assertEqual(coord.txns["T"].decision, "abort")
            parts["p3"].restart(sim)
            sim.run(50)
            self.assertEqual(parts["p3"].decision_of("T"), "abort")
            assert_consistent(self, coord, parts)


class TestParticipantFailures(unittest.TestCase):
    def test_crash_after_yes_vote_blocks_then_aborts(self):
        with tempfile.TemporaryDirectory() as d:
            sim, coord, parts = make_cluster(d)
            # p3 can receive PREPARE but its VOTE never reaches coord
            sim.faults.partitions.add(("p3", COORD))
            coord.begin(sim, "T", PARTS)
            sim.run(1)
            self.assertIn("T", parts["p3"].blocked())
            sim.faults.partitions.add((COORD, "p3"))  # fully partitioned now
            sim.run(20)  # coordinator times out -> abort, but p3 unreachable
            self.assertEqual(coord.txns["T"].decision, "abort")
            self.assertIn("T", parts["p3"].blocked())  # still blocked
            # heal the partition
            sim.faults.partitions.clear()
            sim.run(50)
            self.assertEqual(parts["p3"].decision_of("T"), "abort")
            self.assertEqual(parts["p3"].blocked(), {})
            assert_consistent(self, coord, parts)

    def test_participant_restart_recovers_prepared_state(self):
        with tempfile.TemporaryDirectory() as d:
            sim, coord, parts = make_cluster(d)
            sim.faults.partitions.add(("p1", COORD))  # p1's vote is held up
            coord.begin(sim, "T", PARTS)
            sim.run(1)
            parts["p1"].crash()
            parts["p1"].restart(sim)
            # prepared state survived the crash via the log
            self.assertIn("T", parts["p1"].blocked())
            sim.faults.partitions.clear()
            sim.run(50)
            self.assertEqual(parts["p1"].decision_of("T"), "commit")
            assert_consistent(self, coord, parts)


class TestCoordinatorFailures(unittest.TestCase):
    def test_kill_after_decision_restart_completes(self):
        with tempfile.TemporaryDirectory() as d:
            sim, coord, parts = make_cluster(d)
            coord.begin(sim, "T", PARTS)
            sim.run(2)  # decision persisted and partly delivered
            coord.crash()
            sim.run(10)
            coord.restart(sim)
            sim.run(50)
            self.assertEqual(coord.txns["T"].decision, "commit")
            self.assertEqual(set(decisions(parts, "T").values()), {"commit"})
            self.assertTrue(coord.txns["T"].done)

    def test_kill_before_decision_presumes_abort(self):
        with tempfile.TemporaryDirectory() as d:
            sim, coord, parts = make_cluster(d)
            # silence everyone so no votes arrive
            sim.faults.partitions.add(("*", COORD))
            coord.begin(sim, "T", PARTS)
            sim.run(1)
            coord.crash()
            sim.faults.partitions.clear()
            coord.restart(sim)  # no decide record -> presume abort
            sim.run(50)
            self.assertEqual(coord.txns["T"].decision, "abort")
            self.assertEqual(set(decisions(parts, "T").values()), {"abort"})
            assert_consistent(self, coord, parts)

    def test_repeated_kills_still_converge(self):
        with tempfile.TemporaryDirectory() as d:
            sim, coord, parts = make_cluster(d)
            coord.begin(sim, "T", PARTS)
            for _ in range(3):
                sim.run(1.5)
                coord.crash()
                coord.restart(sim)
            sim.run(60)
            assert_consistent(self, coord, parts)
            self.assertTrue(coord.txns["T"].done)


class TestNetworkFaults(unittest.TestCase):
    def test_drop_dup_delay_reorder(self):
        with tempfile.TemporaryDirectory() as d:
            faults = Faults(drop=0.2, dup=0.3, min_delay=0.0, max_delay=3.0)
            sim, coord, parts = make_cluster(d, seed=7, faults=faults,
                                             vote_timeout=60.0)
            coord.begin(sim, "T", PARTS)
            sim.run(300)
            self.assertTrue(coord.txns["T"].done)
            self.assertEqual(set(decisions(parts, "T").values()), {"commit"})

    def test_duplicate_decision_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            sim, coord, parts = make_cluster(d)
            coord.begin(sim, "T", PARTS)
            sim.run(50)
            p = parts["p1"]
            before = len(p.store.read_all()[0])
            # deliver the same COMMIT three more times
            for _ in range(3):
                p.on_message({"type": "DECISION", "txn": "T",
                              "decision": "commit", "src": COORD}, sim)
            sim.run(10)
            self.assertEqual(p.decision_of("T"), "commit")
            records = p.store.read_all()[0]
            self.assertEqual(len(records), before)  # nothing re-persisted
            self.assertEqual(
                sum(1 for r in records if r["op"] == "decision"), 1)

    def test_contradictory_decision_refused(self):
        with tempfile.TemporaryDirectory() as d:
            sim, coord, parts = make_cluster(d)
            coord.begin(sim, "T", PARTS)
            sim.run(50)
            p = parts["p1"]
            p.on_message({"type": "DECISION", "txn": "T",
                          "decision": "abort", "src": COORD}, sim)
            self.assertEqual(p.decision_of("T"), "commit")  # unchanged
            ops = [r["op"] for r in p.store.read_all()[0]]
            self.assertIn("conflict", ops)


class TestDoctor(unittest.TestCase):
    def _corrupt(self, path):
        with open(path, "ab") as f:
            f.write(b'{"op": "decide", "txn": "T", "decisi')  # truncated

    def test_corrupt_coordinator_log_commit_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            faults = Faults(min_delay=0.0, max_delay=2.0)
            sim, coord, parts = make_cluster(d, seed=3, faults=faults)
            coord.begin(sim, "T", PARTS)
            # run until the commit decision is persisted, then kill p3
            # before its DECISION message is delivered
            while coord.txns["T"].decision is None:
                sim.step()
            self.assertEqual(coord.txns["T"].decision, "commit")
            parts["p3"].crash()
            sim.run(20)
            self.assertEqual(parts["p1"].decision_of("T"), "commit")
            parts["p3"].restart(sim)
            self.assertIn("T", parts["p3"].blocked())
            self._corrupt(os.path.join(d, COORD + ".log"))
            # automatic recovery must refuse to guess
            coord.crash()
            with self.assertRaises(LogCorruptError):
                coord.restart(sim)
            report = analyze(d)
            e = report["transactions"]["T"]
            self.assertEqual(e["suggestion"], "commit")
            self.assertIn(COORD, report["corrupt"])

    def test_no_decision_anywhere_suggests_abort(self):
        with tempfile.TemporaryDirectory() as d:
            sim, coord, parts = make_cluster(d)
            sim.faults.partitions.add(("*", COORD))  # votes never return
            coord.begin(sim, "T", PARTS)
            sim.run(1)  # participants prepared, no votes returned
            os.remove(os.path.join(d, COORD + ".log"))  # log totally lost
            report = analyze(d)
            e = report["transactions"]["T"]
            self.assertEqual(e["suggestion"], "abort")
            self.assertEqual(e["confidence"], "medium")

    def test_conflicting_evidence_no_suggestion(self):
        with tempfile.TemporaryDirectory() as d:
            sim, coord, parts = make_cluster(d)
            coord.begin(sim, "T", PARTS)
            sim.run(50)  # everyone committed
            # forge abort evidence in p3's log (e.g. disk corruption artifact)
            parts["p3"].store.append({"op": "decision", "txn": "T",
                                      "decision": "abort"})
            report = analyze(d)
            e = report["transactions"]["T"]
            self.assertIsNone(e["suggestion"])
            self.assertEqual(e["confidence"], "none")


class TestFuzz(unittest.TestCase):
    def test_random_faults_and_crashes_never_inconsistent(self):
        for seed in range(30):
            with tempfile.TemporaryDirectory() as d:
                rng = random.Random(seed)
                faults = Faults(drop=0.15, dup=0.15, min_delay=0.0, max_delay=3.0)
                votes = {"p3": "no"} if seed % 7 == 0 else {}
                sim, coord, parts = make_cluster(d, seed=seed, faults=faults,
                                                 votes=votes)
                coord.begin(sim, "T", PARTS)
                # random crash/restart events
                events = []
                nodes = [coord] + list(parts.values())
                for _ in range(4):
                    node = rng.choice(nodes)
                    t1 = rng.uniform(0, 8)
                    events.append((t1, node, "crash"))
                    events.append((t1 + rng.uniform(1, 5), node, "restart"))
                # random temporary partition
                a, b = rng.sample([COORD] + PARTS, 2)
                pt1 = rng.uniform(0, 6)
                events.append((pt1, (a, b), "partition"))
                events.append((pt1 + rng.uniform(1, 4), (a, b), "heal"))
                events.sort(key=lambda e: e[0])

                while sim.time < 80:
                    while events and events[0][0] <= sim.time:
                        _, node, kind = events.pop(0)
                        if kind == "crash":
                            node.crash()
                        elif kind == "restart":
                            node.restart(sim)
                        elif kind == "partition":
                            sim.faults.partitions.add(node)
                            sim.faults.partitions.add((node[1], node[0]))
                        elif kind == "heal":
                            sim.faults.partitions.discard(node)
                            sim.faults.partitions.discard((node[1], node[0]))
                    if not sim.step():
                        break
                # make sure everyone is alive at the end and let it settle
                for n in nodes:
                    if not n.alive:
                        n.restart(sim)
                sim.faults.partitions.clear()
                sim.run(200)
                assert_consistent(self, coord, parts)
                # with healed faults and retries, the txn must terminate
                self.assertTrue(coord.txns["T"].done,
                                "seed %d: coordinator not done" % seed)
                for p in parts.values():
                    self.assertIsNotNone(p.decision_of("T"),
                                         "seed %d: %s still blocked"
                                         % (seed, p.name))


if __name__ == "__main__":
    unittest.main()
