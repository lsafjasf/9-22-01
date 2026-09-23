"""Integration tests: run the example scripts through the CLI."""

import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_example(name, *args):
    return subprocess.run(
        [sys.executable, "-m", "minivm", "run",
         os.path.join("examples", name)] + list(args),
        cwd=ROOT, capture_output=True, text=True, timeout=120)


class ExamplesTest(unittest.TestCase):
    def test_closures(self):
        r = run_example("closures.asm")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("c1= 3", r.stdout)
        self.assertIn("c2= 2", r.stdout)

    def test_trap(self):
        r = run_example("trap.asm")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("caught: division by zero", r.stdout)
        self.assertIn("caught raise: boom", r.stdout)
        self.assertIn("=> recovered", r.stdout)

    def test_cycles(self):
        r = run_example("cycles.asm")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("finalized after full gc: 10", r.stdout)

    def test_crossgen(self):
        r = run_example("crossgen.asm")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("holder.slot.n= 999", r.stdout)

    def test_longrun_memory_stable(self):
        r = run_example("longrun.asm")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("finalized objects: 1030", r.stdout)
        self.assertIn("minor collections (copying)", r.stdout)
        self.assertIn("major collections (mark&sweep)", r.stdout)
        self.assertIn("reachable objects now", r.stdout)
        # 200012 objects allocated, but the live set stays tiny
        self.assertIn("objects allocated (total)   : 200012", r.stdout)


if __name__ == "__main__":
    unittest.main()
