"""Runnable demonstration: legacy order vs HLC order under bad clocks.

No real sleeping: a scripted clock steps backwards to emulate an NTP step /
DST end / restart on a skewed host.

    python3 demo.py
"""

from eventorder import HybridClock, merge as fixed_merge
from eventorder import legacy

from tests.helpers import FakeClock


def show(title, order):
    print("%-34s %s" % (title, " -> ".join(e["id"] for e in order)))


def main():
    bad_clock_1 = FakeClock([1000, 900])
    bad_clock_2 = FakeClock([400])

    a0 = legacy.stamp({"id": "A"}, "s1", clock=bad_clock_1.time)
    b0 = legacy.stamp({"id": "B"}, "s1", clock=bad_clock_1.time)
    c0 = legacy.stamp({"id": "C"}, "s2", clock=bad_clock_2.time)
    show("legacy wall-clock merge:", legacy.merge([a0, c0], [b0]))

    hc1 = HybridClock("s1", origin="s1-001", clock=bad_clock_1.time)
    hc2 = HybridClock("s2", origin="s2-001", clock=bad_clock_2.time)
    a = hc1.stamp({"id": "A", "ts": 1000})
    b = hc1.stamp({"id": "B", "ts": 900})
    hc2.observe(b)
    c = hc2.stamp({"id": "C", "ts": 400})
    show("fixed HLC merge:", fixed_merge([a, c], [b]))
    print()
    print("raw wall timestamps retained for audit:",
          {e["id"]: e["ts"] for e in (a, b, c)})


if __name__ == "__main__":
    main()
