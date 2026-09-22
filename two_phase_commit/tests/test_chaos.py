"""Randomized mixed-fault fuzzing: every run must finish consistently."""

import random
import unittest

from two_phase_commit.sim import SimNetwork
from two_phase_commit.tests.test_helpers import Harness


class _NoopCase:
    def addCleanup(self, fn, *a, **k):
        pass

    def assertFalse(self, cond, msg=""):
        if cond:
            raise AssertionError(msg)

    def assertEqual(self, a, b, msg=""):
        if a != b:
            raise AssertionError("%r != %r %s" % (a, b, msg))


def run_chaos(seed, n=3):
    rng = random.Random(seed)
    net = SimNetwork(
        latency=0.005,
        jitter=0.03,
        loss=rng.choice([0.0, 0.05, 0.1, 0.2]),
        duplicate=rng.choice([0.0, 0.1, 0.3]),
        reorder=rng.choice([0.0, 0.1, 0.3]),
        seed=seed,
    )
    no_voter = rng.choice([None, "p%d" % rng.randrange(n)])
    h = Harness(_NoopCase(), n=n, seed=seed, net=net,
                no_voters=(no_voter,) if no_voter else ())
    h.start()

    txns = ["T%d" % i for i in range(rng.randrange(1, 4))]
    for i, txn in enumerate(txns):
        h.begin(txn, at=0.1 + 0.5 * i)

    # Paired crash/restart schedule: every kill has a boot, nodes already
    # down are skipped, and the last action in history is always a boot
    # followed by a long crash-free convergence window.
    crash_window_end = 3.0 + 0.5 * len(txns)
    down_until = {}
    t = 0.3 + 0.5 * len(txns)
    for _ in range(rng.randrange(3, 6)):
        candidates = [
            x for x in (h.pids + ["coordinator"]) if down_until.get(x, 0) <= t
        ]
        if not candidates:
            t += 0.3
            continue
        target = rng.choice(candidates)
        down = rng.uniform(0.3, 1.0)
        boot_at = min(t + down, crash_window_end)
        h.kill(target, t)
        h.boot(target, boot_at)
        down_until[target] = boot_at
        t += rng.uniform(0.3, 0.8)

    # Transient partitions, all healed inside the crash window.
    for _ in range(rng.randrange(0, 3)):
        pid = rng.choice(h.pids)
        start = rng.uniform(0.4, crash_window_end - 0.6)
        heal_at = min(start + rng.uniform(0.3, 1.0), crash_window_end)
        h.partition("coordinator", pid, start)
        h.heal("coordinator", pid, heal_at)

    # Guarantee everyone is back after the crash window.
    for node_id in h.pids + ["coordinator"]:
        h.boot(node_id, crash_window_end + 0.1)
    h.run(budget=crash_window_end + 8.0)

    for txn in txns:
        h.assert_uniform(txn, expected="ABORT" if no_voter else None)
    return h


class ChaosTest(unittest.TestCase):
    def test_many_randomized_histories_are_consistent(self):
        for seed in range(60):
            with self.subTest(seed=seed):
                run_chaos(seed)


if __name__ == "__main__":
    unittest.main()
