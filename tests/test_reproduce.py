"""Tests that stably reproduce the original ordering defects.

These assert the *buggy* behaviour of ``eventorder.legacy`` using an injected
clock, so they fail/hold without sleeping or depending on machine timing.
The fixed implementation must make every one of these scenarios causal,
which is asserted separately in ``test_regression.py``.
"""

import unittest

from eventorder import legacy

from tests.helpers import FakeClock


def payload(name):
    return {"id": name}


class ReproduceDefectsTest(unittest.TestCase):
    def test_clock_goes_backwards_newer_event_sorts_first(self):
        # B happens after A (A was already observed), but its wall clock
        # was stepped back, so naive ts ordering puts B before A.
        clock = FakeClock([1000, 900])
        a = legacy.stamp(payload("A"), "s1", clock=clock.time)
        b = legacy.stamp(payload("B"), "s1", clock=clock.time)
        self.assertLess(b["ts"], a["ts"])  # sanity: wall regressed
        merged = legacy.merge([a], [b])
        self.assertEqual([e["id"] for e in merged], ["B", "A"])
        # The observed causal order A -> B is violated.
        self.assertNotEqual(
            [e["id"] for e in merged], ["A", "B"],
            "legacy defect unexpectedly gone",
        )

    def test_multiple_events_within_one_millisecond_tie(self):
        # Three events inside one millisecond share one timestamp.  Merge
        # output depends solely on call/insertion order (stable sort), not
        # on source or sequence, so swapping the streams flips the order.
        clock = FakeClock([1000])
        a = legacy.stamp(payload("A"), "s1", clock=clock.time)
        b = legacy.stamp(payload("B"), "s2", clock=clock.time)
        c = legacy.stamp(payload("C"), "s3", clock=clock.time)
        first = legacy.merge([a], [b], [c])
        swapped = legacy.merge([c], [b], [a])
        self.assertEqual([e["id"] for e in first], ["A", "B", "C"])
        self.assertNotEqual(
            [e["id"] for e in swapped],
            ["A", "B", "C"],
            "same-ms total order must not be reproducible under legacy merge",
        )

    def test_restart_on_skewed_clock_regresses_timestamp(self):
        # Process stamps A, restarts on a host whose clock is behind, stamps B.
        clock = FakeClock([2000, 1500])
        a = legacy.stamp(payload("A"), "s1", clock=clock.time)
        b = legacy.stamp(payload("B"), "s1", clock=clock.time)
        merged = legacy.merge([a, b])
        self.assertEqual([e["id"] for e in merged], ["B", "A"])

    def test_cross_source_merge_ignores_observed_order(self):
        # Source s1 observes A, then sends a message to s2; s2 reacts with B.
        # s2's wall clock lags, so B sorts before A despite A -> B causality.
        clock1 = FakeClock([5000])
        clock2 = FakeClock([4000])
        a = legacy.stamp(payload("A"), "s1", clock=clock1.time)
        b = legacy.stamp(payload("B"), "s2", clock=clock2.time)
        merged = legacy.merge([a], [b])
        self.assertEqual([e["id"] for e in merged], ["B", "A"])


if __name__ == "__main__":
    unittest.main()
