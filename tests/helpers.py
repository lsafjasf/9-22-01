"""Shared test utilities: a scripted, injectable clock (no real sleeping)."""


class FakeClock:
    """Clock whose wall time is scripted in milliseconds.

    ``values`` are consumed one per ``time()`` call; when exhausted the clock
    stays at the last value.  Time may jump backwards or forwards arbitrarily,
    modelling NTP steps, DST wall changes and restarts on skewed machines.
    """

    def __init__(self, values):
        self._values = list(values)
        self.calls = 0

    def time(self):
        idx = min(self.calls, len(self._values) - 1)
        value = self._values[idx]
        self.calls += 1
        return value / 1000.0
