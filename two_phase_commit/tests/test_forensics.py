"""Tests for the read-only recovery tool."""

import json
import os
import subprocess
import sys
import tempfile
import unittest

from two_phase_commit import forensics
from two_phase_commit.storage import DurableLog, corrupt_tail


def write_log(path, records):
    log = DurableLog(path)
    for rtype, payload in records:
        log.append(rtype, payload)
    log.close()


class ForensicsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="tpc-for-")

    def coord(self):
        return os.path.join(self.dir, "coordinator.log")

    def part(self, pid):
        return os.path.join(self.dir, "participant-%s.log" % pid)

    def analyze(self, txn="T1"):
        paths = [self.coord()] + [self.part(p) for p in ("a", "b", "c")]
        return forensics.inspect(paths, txns=[txn])[0]

    def test_participant_apply_commit_forces_commit(self):
        write_log(self.coord(), [("BEGIN", {"txn": "T1"})])
        write_log(self.part("a"), [
            ("VOTE", {"txn": "T1", "vote": "VOTE_COMMIT"}),
            ("APPLY", {"txn": "T1", "outcome": "COMMIT"}),
        ])
        write_log(self.part("b"), [
            ("VOTE", {"txn": "T1", "vote": "VOTE_COMMIT"})])
        write_log(self.part("c"), [])
        r = self.analyze()
        self.assertEqual(r.recommendation, "FORCE COMMIT")
        self.assertTrue(r.safe)
        self.assertIn("participant-a", r.committed_participants)

    def test_torn_coordinator_log_is_ambiguous_never_auto_guessed(self):
        write_log(self.coord(), [
            ("BEGIN", {"txn": "T1"}),
            ("DECISION", {"txn": "T1", "decision": "COMMIT"}),
        ])
        corrupt_tail(self.coord(), mode="truncate")
        for p in ("a", "b", "c"):
            write_log(self.part(p), [
                ("VOTE", {"txn": "T1", "vote": "VOTE_COMMIT"})])
        r = self.analyze()
        self.assertIn("AMBIGUOUS", r.recommendation)
        self.assertFalse(r.safe)
        self.assertTrue(any("MANUAL" in a or "human" in a or "EVERY" in a
                            for a in r.actions))

    def test_intact_coordinator_without_decision_is_presumed_abort(self):
        write_log(self.coord(), [("BEGIN", {"txn": "T1"})])
        for p in ("a", "b", "c"):
            write_log(self.part(p), [
                ("VOTE", {"txn": "T1", "vote": "VOTE_COMMIT"})])
        r = self.analyze()
        self.assertIn("ABORT", r.recommendation)
        self.assertTrue(r.safe)

    def test_durable_abort_plus_applied_abort_recommends_abort(self):
        write_log(self.coord(), [
            ("BEGIN", {"txn": "T1"}),
            ("DECISION", {"txn": "T1", "decision": "ABORT"}),
        ])
        write_log(self.part("a"), [
            ("VOTE", {"txn": "T1", "vote": "VOTE_ABORT"}),
            ("APPLY", {"txn": "T1", "outcome": "ABORT"}),
        ])
        write_log(self.part("b"), [])
        write_log(self.part("c"), [])
        r = self.analyze()
        self.assertIn("ABORT", r.recommendation)
        self.assertTrue(r.safe)

    def test_missing_participant_log_is_insufficient_evidence(self):
        write_log(self.coord(), [("BEGIN", {"txn": "T1"})])
        corrupt_tail(
            self._write_partial_coord(), mode="truncate")
        write_log(self.part("a"), [
            ("VOTE", {"txn": "T1", "vote": "VOTE_COMMIT"})])
        # b and c logs do not exist
        r = self.analyze()
        self.assertIn("INSUFFICIENT", r.recommendation)
        self.assertFalse(r.safe)

    def _write_partial_coord(self):
        write_log(self.coord(), [
            ("BEGIN", {"txn": "T1"}),
            ("DECISION", {"txn": "T1", "decision": "COMMIT"}),
        ])
        return self.coord()

    def test_inspector_does_not_mutate_logs(self):
        write_log(self.coord(), [("BEGIN", {"txn": "T1"})])
        for p in ("a", "b", "c"):
            write_log(self.part(p), [
                ("VOTE", {"txn": "T1", "vote": "VOTE_COMMIT"})])
        before = {}
        for name in os.listdir(self.dir):
            with open(os.path.join(self.dir, name), "rb") as fh:
                before[name] = fh.read()
        self.analyze()
        after = {}
        for name in os.listdir(self.dir):
            with open(os.path.join(self.dir, name), "rb") as fh:
                after[name] = fh.read()
        self.assertEqual(before, after)


class CliSmokeTest(unittest.TestCase):
    def test_inspect_cli_json_exit_codes(self):
        d = tempfile.mkdtemp(prefix="tpc-cli-")
        coord = os.path.join(d, "coordinator.log")
        write_log(coord, [("BEGIN", {"txn": "T1"})])
        for p in ("a", "b"):
            write_log(os.path.join(d, "participant-%s.log" % p), [
                ("VOTE", {"txn": "T1", "vote": "VOTE_COMMIT"})])
        repo = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        proc = subprocess.run(
            [sys.executable, "-m", "two_phase_commit.cli",
             "inspect", d, "--json"],
            cwd=repo, capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        self.assertEqual(data[0]["txn"], "T1")
        self.assertIn("ABORT", data[0]["recommendation"])


if __name__ == "__main__":
    unittest.main()
