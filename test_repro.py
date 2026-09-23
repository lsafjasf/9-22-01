"""缺陷复现测试：每个用例先证明旧实现乱序，再证明修复版保持因果。

运行：python3 -m unittest test_repro -v
"""

import os
import tempfile
import unittest

import event_order_buggy as buggy
import event_order as fixed


class FakeClock:
    """可控制的假墙上时钟（秒）。"""
    def __init__(self, t):
        self.t = float(t)

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt

    def rollback(self, dt):
        self.t -= dt


class ClockRollbackRepro(unittest.TestCase):
    def test_clock_rollback(self):
        wall = FakeClock(1000.0)
        b1 = buggy.stamp("first", "svc", clock=wall)
        wall.rollback(100.0)  # NTP 校正导致时钟回拨
        b2 = buggy.stamp("second", "svc", clock=wall)
        ordered = buggy.sort_events([b1, b2])
        self.assertIs(ordered[-1], b1, "缺陷复现：回拨后后发生的事件被排到前面")

        wall = FakeClock(1000.0)
        clock = fixed.EventClock("svc", clock=wall)
        e1 = clock.stamp("first")
        wall.rollback(100.0)
        e2 = clock.stamp("second")
        self.assertLess(e1.hlc.key(), e2.hlc.key())
        self.assertEqual(fixed.sort_events([e2, e1]), [e1, e2])


class SameMillisecondRepro(unittest.TestCase):
    def test_same_millisecond_events(self):
        wall = FakeClock(1000.0)  # 时钟冻结在同一毫秒
        b_events = [buggy.stamp(i, "svc", clock=wall) for i in range(5)]
        self.assertEqual(len({e.timestamp for e in b_events}), 1,
                         "缺陷复现：同一毫秒内 5 个事件时间戳完全相同，无法区分先后")

        clock = fixed.EventClock("svc", clock=wall)
        events = [clock.stamp(i) for i in range(5)]
        keys = [e.hlc.key() for e in events]
        self.assertEqual(len(set(keys)), 5)
        self.assertEqual(keys, sorted(keys), "同刻事件必须按发生顺序严格递增")


class RestartRegressionRepro(unittest.TestCase):
    def test_restart_timestamp_regression(self):
        b1 = buggy.stamp("before-restart", "svc", clock=lambda: 1000.0)
        b2 = buggy.stamp("after-restart", "svc", clock=lambda: 500.0)
        ordered = buggy.sort_events([b1, b2])
        self.assertIs(ordered[0], b2, "缺陷复现：重启后墙上时间倒退，新事件排到旧事件前")

        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "clock.json")
            wall = FakeClock(1000.0)
            c1 = fixed.EventClock("svc", clock=wall, state_path=state)
            e1 = c1.stamp("before-restart")
            c1.close()
            wall.rollback(500.0)  # 重启后墙上时间倒退
            c2 = fixed.EventClock("svc", clock=wall, state_path=state)
            e2 = c2.stamp("after-restart")
            self.assertLess(e1.hlc.key(), e2.hlc.key())
            self.assertEqual(fixed.sort_events([e1, e2]), [e1, e2])


class CrossSourceMergeRepro(unittest.TestCase):
    def test_cross_source_merge(self):
        # B 的墙上时钟比 A 慢 50 秒（时钟偏差）
        b1 = buggy.stamp("A 的事件", "A", clock=lambda: 1000.0)
        b2 = buggy.stamp("B 观察到 A 之后的事件", "B", clock=lambda: 950.0)
        merged = list(buggy.merge_streams([[b1], [b2]]))
        self.assertIs(merged[0], b2, "缺陷复现：时钟偏差使因果顺序被打乱")

        wall_a, wall_b = FakeClock(1000.0), FakeClock(950.0)
        a = fixed.EventClock("A", clock=wall_a)
        b = fixed.EventClock("B", clock=wall_b)
        e1 = a.stamp("A 的事件")
        e2 = b.stamp("B 观察到 A 之后的事件", after=e1)  # 显式因果依赖
        merged = list(fixed.merge_streams([[e1], [e2]]))
        self.assertEqual(merged, [e1, e2])
        self.assertLess(e1.hlc.key(), e2.hlc.key())


if __name__ == "__main__":
    unittest.main()
