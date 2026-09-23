"""Legacy event-ordering implementation (BUGGY, kept only for reproduction).

The old design stamps events with the local wall clock at emit time and later
merges streams purely by that timestamp.  It has no concept of causality and
no deterministic tie breaker, so clock adjustments, restarts and same-ms
bursts all produce wrong orders.
"""

import itertools
import time

_seq = itertools.count()


def now_ms(clock=time.time):
    return int(clock() * 1000)


def stamp(event, source, clock=time.time):
    """Mutate and return ``event`` with a wall-clock timestamp."""
    event["ts"] = now_ms(clock)
    event["source"] = source
    return event


def merge(*streams):
    """Merge event streams by raw wall timestamp only.

    Python's sort is stable, but input ordering across sources is an accident
    of call order, so ties are not reproducible across restarts/rearrangements.
    """
    events = []
    for stream in streams:
        events.extend(stream)
    events.sort(key=lambda e: e["ts"])
    return events
