"""修复版回归测试：因果性、确定性、边界条件、内存开销、接口兼容。

运行：python3 -m unittest test_regression -v
"""

import gc
import unittest
import warnings

import event_order_buggy as buggy
import event_order as fixed
from test_repro import FakeClock


class CausalityGuarantee(unittest.TestCase):
    def test_observed_order_preserved_across_sources(self):
        # 三个来源，时钟各自有偏差；因果链 e1 -> e2 -> e3 必须保持
        a = fixed.EventClock("A", clock=FakeClock(1000.0))
        b = fixed.EventClock("B", clock=FakeClock(900.0))
        c = fixed.EventClock("C", clock=FakeClock(1100.0))
        e1 = a.stamp("e1")
        e2 = b.stamp("e2", after=e1)
        e3 = c.stamp("e3", after=e2)
        merged = list(fixed.merge_streams([[e3], [e1], [e2]]))  # 乱序输入流
        self.assertEqual(merged, [e1, e2, e3])

    def test_concurrent_events_have_deterministic_total_order(self):
        # 并发事件（无因果关系）：同一物理毫秒、不同来源
        wall = FakeClock(1000.0)
        clocks = [fixed.EventClock(name, clock=wall) for name in ("B", "A", "C")]
        events = [clk.stamp(i) for i, clk in enumerate(clocks)]
        order1 = [e.source for e in fixed.sort_events(events)]
        order2 = [e.source for e in fixed.sort_events(list(reversed(events)))]
        self.assertEqual(order1, order2, "并发事件的排序必须确定且与输入顺序无关")
        self.assertEqual(order1, ["A", "B", "C"], "并发事件按来源标识仲裁")

    def test_merge_is_reproducible(self):
        a = fixed.EventClock("A", clock=FakeClock(1000.0))
        b = fixed.EventClock("B", clock=FakeClock(1000.0))
        ea = [a.stamp(("a", i)) for i in range(50)]
        eb = [b.stamp(("b", i)) for i in range(50)]
        run1 = [e.payload for e in fixed.merge_streams([ea, eb])]
        run2 = [e.payload for e in fixed.merge_streams([ea, eb])]
        self.assertEqual(run1, run2)


class EdgeCases(unittest.TestCase):
    def test_missing_wall_timestamp(self):
        clock = fixed.EventClock("svc", clock=FakeClock(1000.0))
        e1 = fixed.Event("no-wall-time", "svc")          # wall_time 与 hlc 均缺失
        e2 = clock.stamp("normal", wall_time=None)       # 仅 wall_time 缺失
        ordered = fixed.sort_events([e2, e1])            # 不应抛异常
        self.assertEqual(ordered, [e2, e1])              # e2 的 HLC 更早，排前面
        self.assertIsNone(e1.wall_time)                  # 原始字段保持原样
        self.assertIsNotNone(e1.display_time)            # 展示时间有兜底

    def test_missing_timestamp_legacy_events_keep_observed_order(self):
        legacy = [fixed.Event(i, "old") for i in range(10)]  # 全部无 hlc
        ordered = fixed.sort_events(legacy)
        self.assertEqual([e.payload for e in ordered], list(range(10)),
                         "无戳事件按被观察到的顺序排列")

    def test_duplicate_source_ids(self):
        wall = FakeClock(1000.0)
        c1 = fixed.EventClock("dup", clock=wall)
        c2 = fixed.EventClock("dup", clock=wall)  # 来源标识重复
        e1 = [c1.stamp(("s1", i)) for i in range(10)]
        e2 = [c2.stamp(("s2", i)) for i in range(10)]
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            run1 = [e.payload for e in fixed.merge_streams([e1, e2])]
        self.assertTrue(any("重复" in str(w.message) for w in caught))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            run2 = [e.payload for e in fixed.merge_streams([e1, e2])]
        self.assertEqual(run1, run2, "重复来源下排序仍须确定可重现")
        self.assertEqual(len(run1), 20)

    def test_thread_safety(self):
        import threading
        clock = fixed.EventClock("svc", clock=FakeClock(1000.0))
        out = []
        def worker():
            for _ in range(500):
                out.append(clock.now())
        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads: t.start()
        for t in threads: t.join()
        keys = [h.key() for h in out]
        self.assertEqual(len(set(keys)), len(keys), "并发打戳不得重复")


class MemoryAndScale(unittest.TestCase):
    def test_merge_is_lazy(self):
        consumed = [0, 0]
        def gen(idx, n):
            for i in range(n):
                consumed[idx] += 1
                yield fixed.Event((idx, i), "s%d" % idx,
                                  hlc=fixed.HLCTimestamp(i, 0, "s%d" % idx))
        merged = fixed.merge_streams([gen(0, 10**9), gen(1, 10**9)])
        for _ in range(10):
            next(merged)
        self.assertLessEqual(max(consumed), 11,
                             "合并必须是惰性的：只消费已产出的少量事件")

    def test_merge_large_streams(self):
        n = 100_000
        def gen(src):
            for i in range(n):
                yield fixed.Event((src, i), src,
                                  hlc=fixed.HLCTimestamp(i, 0, src))
        merged = list(fixed.merge_streams([gen("a"), gen("b"), gen("c")]))
        self.assertEqual(len(merged), 3 * n)
        keys = [(e.hlc.physical, e.hlc.node) for e in merged]
        self.assertEqual(keys, sorted(keys))

    def test_event_slots_keep_memory_low(self):
        e = fixed.Event("x", "s")
        self.assertFalse(hasattr(e, "__dict__"), "Event 应使用 __slots__")


class InterfaceCompat(unittest.TestCase):
    def test_public_api_matches_buggy_version(self):
        for name in ("Event", "stamp", "sort_events", "merge_streams"):
            self.assertTrue(hasattr(buggy, name))
            self.assertTrue(hasattr(fixed, name))

    def test_event_timestamp_property_and_wall_time_kept(self):
        clock = fixed.EventClock("svc", clock=FakeClock(1234.5))
        e = clock.stamp("x")
        self.assertEqual(e.timestamp, 1234.5)   # 旧接口属性仍可用
        self.assertEqual(e.wall_time, 1234.5)   # 原始墙上时间保留用于审计
        self.assertEqual(e.display_time, 1234.5)

    def test_module_level_stamp(self):
        e = fixed.stamp("hello", source="svc")
        self.assertIsNotNone(e.hlc)
        self.assertIsNotNone(e.wall_time)


if __name__ == "__main__":
    unittest.main()
