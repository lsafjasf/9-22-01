"""Hybrid Logical Clock (HLC) stamping with durable causal state.

Each event gets an ordering triple ``(hlc_l, hlc_c, seq)`` scoped to a unique
origin.  The original wall timestamp is preserved untouched on ``ts``.

HLC properties used here (Kulkarni et al., 2014):
  * l tracks the largest physical time the origin has learned of;
  * c is a logical counter for events sharing the same l;
  * observing a remote event folds its (l, c) in before stamping, so
    A happened-before B  =>  key(A) < key(B);
  * l stays within clock-skew bounds of real time, unlike a pure Lamport
    counter, which is why audit/display timelines remain meaningful.
"""

import json
import os
import tempfile
import time
import uuid


def now_ms(clock=time.time):
    """Current physical time in integer UTC milliseconds."""
    return int(clock() * 1000.0)


def order_key(event):
    """Global, deterministic total-order key for a stamped event.

    Tuple order: HLC physical part, HLC counter, origin identity, per-origin
    sequence.  Origin/seq break all remaining ties lexicographically/numerically,
    so concurrent events have exactly one reproducible order regardless of
    which stream or process emits them or how inputs are shuffled.
    """
    if not is_stamped(event):
        raise ValueError("event is missing HLC fields; stamp it before merging")
    return (event["hlc_l"], event["hlc_c"], str(event["origin"]), event["seq"])


def is_stamped(event):
    return all(k in event for k in ("hlc_l", "hlc_c", "origin", "seq"))


class HybridClock:
    """Per-process/host HLC instance, optionally persisted across restarts.

    Parameters
    ----------
    source:
        Human-readable source label written to ``event["source"]``.  Labels
        are *not* assumed unique: ``origin`` is the unique identity and must
        come from a stable state file or be supplied explicitly.
    state_path:
        If given, state (origin + last HLC) is atomically loaded on
        construction and saved on every stamp/observe, surviving restarts so
        timestamps never regress even if the wall clock did.
    origin:
        Explicit unique origin id; persisted state wins when both are given.
    clock:
        Injectable physical clock, ``() -> seconds`` (defaults to wall time).
    """

    def __init__(self, source, state_path=None, origin=None, clock=time.time):
        self.source = source
        self._clock = clock
        self.state_path = state_path
        self._seq = 0
        if state_path is not None and os.path.exists(state_path):
            state = self._load()
            self.origin = state["origin"]
            self._l = state["l"]
            self._c = state["c"]
            self._seq = state["seq"]
        else:
            self.origin = origin if origin is not None else uuid.uuid4().hex
            self._l = 0
            self._c = 0
            if state_path is not None:
                self.save()

    # -- HLC primitives -------------------------------------------------

    def _advance(self, remote_l=0, remote_c=0):
        physical = now_ms(self._clock)
        if physical > self._l and physical > remote_l:
            self._l = physical
            self._c = 0
        elif remote_l > self._l:
            self._l = remote_l
            self._c = remote_c + 1
        elif remote_l == self._l:
            self._c = max(self._c, remote_c) + 1
        else:
            self._c += 1

    # -- public API ------------------------------------------------------

    def observe(self, event):
        """Fold an observed (incoming) event into this clock's causal past.

        Call this before reacting to a remote event; any event stamped
        afterwards is guaranteed to order after ``event``.
        """
        if not is_stamped(event):
            raise ValueError("cannot observe an unstamped event")
        if str(event["origin"]) != str(self.origin):
            self._advance(event["hlc_l"], event["hlc_c"])
            if self.state_path is not None:
                self.save()
        return event

    def stamp(self, event):
        """Stamp ``event`` in place, preserving its raw wall timestamp.

        ``event["ts"]`` is left as provided when present (including ``None``);
        only ``hlc_*`` / ``origin`` / ``seq`` / ``source`` are maintained here.
        """
        if is_stamped(event):
            raise ValueError(
                "event already carries HLC fields; use observe() to fold in "
                "causality and stamp a fresh event instead"
            )
        self._advance()
        self._seq += 1
        event.setdefault("ts", None)
        event["hlc_l"] = self._l
        event["hlc_c"] = self._c
        event["origin"] = self.origin
        event["seq"] = self._seq
        event["source"] = self.source
        if self.state_path is not None:
            self.save()
        return event

    def snapshot(self):
        """Return durable clock state as a plain dict."""
        return {
            "origin": self.origin,
            "l": self._l,
            "c": self._c,
            "seq": self._seq,
        }

    # -- persistence ------------------------------------------------------

    def save(self):
        """Atomically replace the state file (write temp + fsync + rename)."""
        if self.state_path is None:
            raise RuntimeError("no state_path configured")
        directory = os.path.dirname(os.path.abspath(self.state_path))
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".hlc-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self.snapshot(), handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.state_path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _load(self):
        with open(self.state_path, "r", encoding="utf-8") as handle:
            state = json.load(handle)
        for key in ("origin", "l", "c", "seq"):
            if key not in state:
                raise ValueError("corrupt HLC state file: missing %r" % key)
        return state
