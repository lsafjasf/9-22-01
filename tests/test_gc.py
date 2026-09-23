"""Unit tests for the generational garbage collector."""

import unittest

from minivm.heap import Heap
from minivm.objects import YOUNG, OLD, HObj, HArray, HStr


class HeapTestCase(unittest.TestCase):
    """A heap whose roots are a plain python list we can mutate freely."""

    def setUp(self):
        self.heap = Heap(young_size=8, old_threshold=16, promote_age=1)
        self.roots = [None] * 4
        self.heap.root_provider = lambda: [(self.roots, i)
                                           for i in range(len(self.roots))]
        self.freed = []

    def finalize(self, obj, tag):
        self.heap.set_finalizer(obj, lambda o, t=tag: self.freed.append(t))

    def test_minor_gc_copies_live_and_reclaims_garbage(self):
        live = self.heap.alloc(HObj())
        live.fields["x"] = 42
        self.roots[0] = live
        dead = self.heap.alloc(HObj())
        self.finalize(dead, "dead")
        self.heap.collect_minor()
        # root slot was rewritten to the copy
        self.assertIsNot(self.roots[0], live)
        self.assertEqual(self.roots[0].fields["x"], 42)
        self.assertEqual(self.freed, ["dead"])
        self.assertEqual(len(self.heap.young), 1)

    def test_cyclic_garbage_collected_young(self):
        a = self.heap.alloc(HObj())
        b = self.heap.alloc(HObj())
        a.fields["peer"] = b
        b.fields["peer"] = a
        self.finalize(a, "a")
        self.finalize(b, "b")
        self.roots[0] = a
        self.roots[0] = None  # drop the only reference
        self.heap.collect_minor()
        self.assertEqual(sorted(self.freed), ["a", "b"])
        self.assertEqual(len(self.heap.young), 0)

    def test_cyclic_garbage_collected_old(self):
        a = self.heap.alloc(HObj())
        b = self.heap.alloc(HObj())
        a.fields["peer"] = b
        b.fields["peer"] = a
        self.roots[0] = a
        self.finalize(a, "a")
        self.finalize(b, "b")
        # promote both to old gen (survive two minor GCs)
        self.heap.collect_minor()
        self.heap.collect_minor()
        self.assertEqual(self.roots[0].gen, OLD)
        self.roots[0] = None
        self.heap.full_collect()
        self.assertEqual(sorted(self.freed), ["a", "b"])
        self.assertEqual(len(self.heap.old), 0)

    def test_live_cycle_survives(self):
        a = self.heap.alloc(HObj())
        b = self.heap.alloc(HObj())
        a.fields["peer"] = b
        b.fields["peer"] = a
        b.fields["payload"] = self.heap.alloc(HStr("hello"))
        self.roots[0] = a
        self.heap.full_collect()
        self.assertEqual(self.roots[0].fields["peer"].fields["payload"].s,
                         "hello")

    def test_cross_gen_reference_write_barrier(self):
        """old -> young store must keep the young object alive across
        minor collections (remembered set)."""
        holder = self.heap.alloc(HObj())
        self.roots[0] = holder
        self.heap.collect_minor()
        self.heap.collect_minor()
        holder = self.roots[0]
        self.assertEqual(holder.gen, OLD)

        child = self.heap.alloc(HObj())
        child.fields["v"] = 7
        self.roots[1] = child
        # the store: write barrier fires, holder enters remembered set
        holder.fields["slot"] = child
        self.heap.write_barrier(holder, child)
        self.assertIn(holder, self.heap.remembered)
        # drop the direct root: only the old object references the child
        self.roots[1] = None
        self.heap.collect_minor()
        self.assertEqual(self.roots[0].fields["slot"].fields["v"], 7)

    def test_without_barrier_young_child_would_be_lost(self):
        """Documents why the barrier is needed: an untracked old->young
        pointer lets the minor collector reclaim a live object."""
        holder = self.heap.alloc(HObj())
        self.roots[0] = holder
        self.heap.collect_minor()
        self.heap.collect_minor()
        holder = self.roots[0]
        child = self.heap.alloc(HObj())
        holder.fields["slot"] = child  # no barrier: remembered set misses it
        self.heap.collect_minor()
        self.assertIsNone(self.roots[0].fields["slot"].forward or None)
        # child was reclaimed (not copied); slot points at a dead object
        self.assertEqual(len(self.heap.young), 0)

    def test_promotion_age(self):
        obj = self.heap.alloc(HObj())
        self.roots[0] = obj
        self.heap.collect_minor()
        self.assertEqual(self.roots[0].gen, YOUNG)  # aged, not yet promoted
        self.assertEqual(self.roots[0].age, 1)
        self.heap.collect_minor()
        self.assertEqual(self.roots[0].gen, OLD)

    def test_major_gc_triggered_by_old_growth(self):
        # keep a bounded live set; garbage gets promoted and must be
        # swept by major collections once the old gen grows
        self.heap.young_size = 4
        for i in range(200):
            junk = self.heap.alloc(HObj())
            self.finalize(junk, "junk%d" % i)
            self.roots[1] = junk  # previous junk becomes floating garbage
        self.roots[1] = None
        self.heap.full_collect()
        self.assertGreater(self.heap.stats.major_collections, 0)
        self.assertLess(len(self.heap.old), 64)
        self.assertEqual(len(self.freed), 200)

    def test_finalizer_runs_once(self):
        obj = self.heap.alloc(HObj())
        self.finalize(obj, "x")
        self.heap.full_collect()
        self.heap.full_collect()
        self.assertEqual(self.freed, ["x"])

    def test_finalizer_survives_copying(self):
        """A release hook must not be lost when the object is copied."""
        obj = self.heap.alloc(HObj())
        self.roots[0] = obj
        self.finalize(obj, "copied")
        self.heap.collect_minor()   # object is copied to to-space
        self.roots[0] = None
        self.heap.collect_minor()
        self.assertEqual(self.freed, ["copied"])

    def test_stats_accounting(self):
        for _ in range(3):
            self.heap.alloc(HObj())
        self.heap.full_collect()
        st = self.heap.stats
        self.assertEqual(st.allocated, 3)
        self.assertGreaterEqual(st.minor_collections, 1)
        self.assertEqual(st.major_collections, 1)
        self.assertGreater(st.minor_pause_s, 0)


if __name__ == "__main__":
    unittest.main()
