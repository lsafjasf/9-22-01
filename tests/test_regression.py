"""Regression tests proving every original defect is fixed and stays fixed.

All timing is scripted through FakeClock: tests are deterministic and run in
milliseconds.  Covered areas: clock regression, same-ms bursts, restart with
persisted state, cross-source causality, DST jumps, missing/None wall
timestamps, duplicate source labels, reproducible total order, bounded memory
for huge volumes (streaming + external sort), and interface compatibility.
"""

import os
import tempfile
import unittest

from eventorder import (
    HybridClock,
    external_merge,
    merge,
    merge_streams,
    order_key,
)

from tests.helpers import FakeClock


def names(events):
    return [e["id"] for e in events]


class ClockRegressionTest(unittest.TestCase):
    def setUp(self):
        self._workdir = tempfile.TemporaryDirectory()
        self.tmpdir = self._workdir.name

    def tearDown(self):
        self._workdir.cleanup()

    def test_clock_rollback_preserves_process_order(self):
        clock = FakeClock([1000, 900])
        hc = HybridClock("s1", origin="o1", clock=clock.time)
        a = hc.stamp({"id": "A"})
        b = hc.stamp({"id": "B"})
        self.assertLess(order_key(a), order_key(b))
        self.assertEqual(names(merge([a], [b])), ["A", "B"])
        self.assertEqual(names(merge([b], [a])), ["A", "B"])

    def test_many_events_same_millisecond_have_strict_total_order(self):
        clock = FakeClock([1000])
        hc = HybridClock("s1", origin="o1", clock=clock.time)
        events = [hc.stamp({"id": i}) for i in range(100)]
        keys = [order_key(e) for e in events]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(len(set(keys)), len(keys))

    def test_same_ms_across_sources_is_deterministic_and_reproducible(self):
        clock = FakeClock([1000])
        hc1 = HybridClock("svc", origin="o1", clock=clock.time)
        hc2 = HybridClock("svc", origin="o2", clock=clock.time)
        hc3 = HybridClock("svc", origin="o3", clock=clock.time)
        a = hc1.stamp({"id": "A"})
        b = hc2.stamp({"id": "B"})
        c = hc3.stamp({"id": "C"})
        expected = names(merge([a], [b], [c]))
        # Identical (l, c); tie broken by origin id, independent of call order
        # and identical across runs / shuffles.
        self.assertEqual(names(merge([c], [b], [a])), expected)
        self.assertEqual(names(merge([b, c, a])), expected)
        self.assertEqual(expected, ["A", "B", "C"])

    def test_restart_with_persisted_state_never_regresses(self):
        state = os.path.join(self.tmpdir, "hlc.state")
        clock = FakeClock([2000])
        hc = HybridClock("s1", state_path=state, origin="o1", clock=clock.time)
        a = hc.stamp({"id": "A"})

        # Restart: new process, wall clock behind (1500), state file reloads
        # last HLC so B still orders after A.
        clock2 = FakeClock([1500])
        hc2 = HybridClock("s1", state_path=state, clock=clock2.time)
        self.assertEqual(hc2.origin, "o1")
        b = hc2.stamp({"id": "B"})
        self.assertLess(order_key(a), order_key(b))
        self.assertEqual(names(merge([a], [b])), ["A", "B"])

    def test_restart_in_memory_only_still_gives_strict_monotonic_order(self):
        # Without a state file a new origin is minted; order remains total and
        # reproducible (it simply cannot claim cross-restart causality).
        clock = FakeClock([2000, 1500])
        hc1 = HybridClock("s1", origin="o1", clock=clock.time)
        a = hc1.stamp({"id": "A"})
        hc2 = HybridClock("s1", origin="o2", clock=clock.time)
        b = hc2.stamp({"id": "B"})
        out = merge([a], [b])
        self.assertEqual(len({order_key(e) for e in out}), 2)

    def test_cross_source_observed_causality_is_preserved(self):
        clock1 = FakeClock([5000])
        clock2 = FakeClock([4000])
        hc1 = HybridClock("s1", origin="o1", clock=clock1.time)
        hc2 = HybridClock("s2", origin="o2", clock=clock2.time)
        a = hc1.stamp({"id": "A"})
        hc2.observe(a)  # s2 receives A before reacting
        b = hc2.stamp({"id": "B"})
        self.assertLess(order_key(a), order_key(b))
        self.assertEqual(names(merge([a], [b])), ["A", "B"])

    def test_fork_join_chain_is_ordered(self):
        # A forks to B and C (concurrent), both observed by D.
        clock = FakeClock([1000])
        hc_a = HybridClock("s1", origin="oa", clock=clock.time)
        hc_b = HybridClock("s2", origin="ob", clock=clock.time)
        hc_c = HybridClock("s3", origin="oc", clock=clock.time)
        hc_d = HybridClock("s4", origin="od", clock=clock.time)
        a = hc_a.stamp({"id": "A"})
        hc_b.observe(a)
        hc_c.observe(a)
        b = hc_b.stamp({"id": "B"})
        c = hc_c.stamp({"id": "C"})
        hc_d.observe(b)
        hc_d.observe(c)
        d = hc_d.stamp({"id": "D"})
        out = merge([a], [b], [c], [d])
        self.assertEqual(names(out)[0], "A")
        self.assertEqual(names(out)[-1], "D")
        self.assertLess(order_key(b), order_key(d))
        self.assertLess(order_key(c), order_key(d))

    def test_dst_wall_jump_does_not_reorder(self):
        # Local wall clock falls back an hour (DST end) between events.
        clock = FakeClock([3_600_000, 0])
        hc = HybridClock("s1", origin="o1", clock=clock.time)
        before = hc.stamp({"id": "before-dst"})
        after = hc.stamp({"id": "after-dst"})
        self.assertEqual(names(merge([before], [after])), ["before-dst", "after-dst"])

    def test_missing_wall_timestamp_is_preserved_as_none(self):
        clock = FakeClock([1000])
        hc = HybridClock("s1", origin="o1", clock=clock.time)
        event = hc.stamp({"id": "no-ts"})
        self.assertIsNone(event["ts"])
        self.assertEqual(event["hlc_l"], 1000)

        # An explicit (even None) wall timestamp is never overwritten.
        event2 = hc.stamp({"id": "explicit", "ts": None})
        self.assertIsNone(event2["ts"])

    def test_duplicate_source_labels_are_disambiguated_by_origin(self):
        clock = FakeClock([1000])
        hc1 = HybridClock("billing", origin="host-7", clock=clock.time)
        hc2 = HybridClock("billing", origin="host-9", clock=clock.time)
        a = hc1.stamp({"id": "A"})
        b = hc2.stamp({"id": "B"})
        self.assertEqual(a["source"], b["source"])
        self.assertNotEqual(a["origin"], b["origin"])
        out = merge([a], [b])
        self.assertEqual(names(out), ["A", "B"])

    def test_original_wall_timestamp_is_retained_for_audit(self):
        clock = FakeClock([1234])
        hc = HybridClock("s1", origin="o1", clock=clock.time)
        event = hc.stamp({"id": "A", "ts": 999})
        self.assertEqual(event["ts"], 999)  # raw wall value untouched
        self.assertEqual(event["hlc_l"], 1234)

    def test_ordering_is_reproducible_across_process_layouts(self):
        clock = FakeClock([1000, 1000, 2000])
        hc1 = HybridClock("s1", origin="o1", clock=clock.time)
        hc2 = HybridClock("s2", origin="o2", clock=clock.time)
        a = hc1.stamp({"id": "A"})
        b = hc2.stamp({"id": "B"})
        c = hc1.stamp({"id": "C"})
        once = names(merge([a, c], [b]))
        # Rebuild identical events from scratch (simulate other processes /
        # reruns): same serialized fields, different call sites.
        copies = [dict(a), dict(b), dict(c)]
        again = names(merge([copies[2]], [copies[0], copies[1]]))
        self.assertEqual(once, again)

    def test_stamp_is_idempotence_guarded(self):
        hc = HybridClock("s1", origin="o1", clock=FakeClock([1, 2]).time)
        event = hc.stamp({"id": "A"})
        with self.assertRaises(ValueError):
            hc.stamp(event)

    def test_randomized_happens_before_edges_are_all_respected(self):
        # Random DAG of local ticks and cross-source messages; every recorded
        # happens-before edge must survive the global merge.
        import random

        rng = random.Random(1234)
        clocks = {
            name: HybridClock(name, origin="origin-" + name,
                              clock=FakeClock(
                                  [1000 + rng.randrange(-50, 50)] * 100_000
                              ).time)
            for name in ("s1", "s2", "s3", "s4")
        }
        all_events = []
        edges = []  # (cause_index, effect_index)
        for _ in range(500):
            hc = clocks[rng.choice(list(clocks))]
            event = hc.stamp({"id": len(all_events)})
            all_events.append(event)
            if all_events and rng.random() < 0.6:
                cause = rng.choice(all_events)
                if cause["origin"] != hc.origin:
                    hc.observe(cause)
                effect = hc.stamp({"id": len(all_events)})
                edges.append((cause["id"], effect["id"]))
                all_events.append(effect)

        merged = merge(all_events)
        position = {event["id"]: i for i, event in enumerate(merged)}
        keys = [order_key(e) for e in merged]
        self.assertEqual(keys, sorted(keys))
        for cause_id, effect_id in edges:
            self.assertLess(position[cause_id], position[effect_id])

    def test_merge_rejects_unstamped_events(self):
        with self.assertRaises(ValueError):
            merge([{"id": "raw", "ts": 1}])
        with self.assertRaises(ValueError):
            list(merge_streams(iter([{"id": "raw"}])))

    def test_observe_requires_stamped_event(self):
        hc = HybridClock("s1", origin="o1", clock=FakeClock([1]).time)
        with self.assertRaises(ValueError):
            hc.observe({"id": "raw"})


class StreamMemoryTest(unittest.TestCase):
    def test_merge_streams_pulls_only_one_event_ahead_per_stream(self):
        # A naive merge materializes every input before emitting.  heapq.merge
        # must hold O(k) events: after n outputs, inputs have been pulled only
        # ~n + k times.
        clock = FakeClock([1])
        h1 = HybridClock("s1", origin="o1", clock=clock.time)
        h2 = HybridClock("s2", origin="o2", clock=clock.time)
        size = 10_000
        ev1 = [h1.stamp({"id": "a%d" % i}) for i in range(size)]
        ev2 = [h2.stamp({"id": "b%d" % i}) for i in range(size)]

        pulled = {"n": 0}

        def counting(events):
            for event in events:
                pulled["n"] += 1
                yield event

        merged = merge_streams(counting(iter(ev1)), counting(iter(ev2)))
        from itertools import islice
        head = list(islice(merged, 10))
        self.assertEqual(len(head), 10)
        # 2 initial prefetch + at most one refill per emitted event.
        self.assertLessEqual(pulled["n"], 12 + 2)
        self.assertEqual(len(list(merged)), 2 * size - 10)
        self.assertEqual(pulled["n"], 2 * size)

    def test_external_merge_spills_and_orders_large_input(self):
        clock = FakeClock([1000])
        hc = HybridClock("s1", origin="o1", clock=clock.time)
        total = 2_500
        events = [hc.stamp({"id": i, "payload": "x" * 32}) for i in range(total)]

        # Split one logical stream into several unsorted iterables, tiny chunks.
        import random
        rng = random.Random(42)
        shuffled = events[:]
        rng.shuffle(shuffled)
        s1 = shuffled[:1000]
        s2 = shuffled[1000:2000]
        s3 = shuffled[2000:]

        with tempfile.TemporaryDirectory() as directory:
            out = list(
                external_merge(
                    iter(s1), iter(s2), iter(s3),
                    chunk_size=250, directory=directory,
                )
            )
            self.assertEqual(len(out), total)
            keys = [order_key(e) for e in out]
            self.assertEqual(keys, sorted(keys))
            self.assertEqual([e["id"] for e in out], list(range(total)))
            # Spill files are removed after the generator finishes.
            self.assertEqual(
                [f for f in os.listdir(directory) if f.endswith(".ndjson")], []
            )


if __name__ == "__main__":
    unittest.main()
