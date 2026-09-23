"""Fixed incremental parser.

Strategy (line-granular reuse):

  * The document is kept as a list of physical lines, each parsed at
    most once into a subtree (``None`` for blank lines).
  * An edit ``[start, end) -> ins`` touches the old lines ``[l0, l1]``
    (the lines containing ``start`` and ``end - 1``).  Exactly those
    lines — mapped into the new text — are re-lexed and re-parsed.
  * Lines before ``l0`` are reused verbatim (their spans cannot move).
  * Lines after ``l1`` are reused as deep copies shifted by
    ``delta = len(ins) - (end - start)`` — the *per-edit* delta, never
    an accumulated one, and never applied to lines before the edit.
  * Error recovery is a pure function of the current text.  There is no
    global or per-document cache, so a failed recovery cannot pollute
    later parses.

Every edit records a ``ReparseInfo`` in ``last_reparse`` so callers can
verify that only the affected range was re-parsed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from parser_core import (
    ErrorInfo,
    Node,
    _line_index_at,
    collect_errors,
    line_spans,
    parse_line,
)


@dataclass(frozen=True)
class ReparseInfo:
    """Statistics about the range re-parsed by one edit (new-text coords)."""
    start: int           # first re-parsed character offset
    end: int             # one past the last re-parsed character offset
    lines_reparsed: int  # number of lines actually re-lexed/re-parsed
    total_lines: int     # number of lines in the whole document
    delta: int           # length change introduced by the edit

    def __str__(self) -> str:  # compact, log-friendly
        return (f"reparse [{self.start}, {self.end}) "
                f"lines={self.lines_reparsed}/{self.total_lines} "
                f"delta={self.delta:+d}")


class IncrementalParser:
    def __init__(self, text: str = ""):
        self._text = ""
        self._spans: List[Tuple[int, int]] = []
        self._nodes: List[Optional[Node]] = []
        self.last_reparse: Optional[ReparseInfo] = None
        self.reset(text)

    # ------------------------------------------------------------------ #

    def reset(self, text: str) -> None:
        """Full re-parse (used for the initial load and degenerate cases)."""
        self._text = text
        self._spans = line_spans(text)
        self._nodes = [parse_line(text, s, e) for s, e in self._spans]
        self.last_reparse = ReparseInfo(0, len(text), len(self._spans),
                                        len(self._spans), 0)

    # ------------------------------------------------------------------ #

    @property
    def text(self) -> str:
        return self._text

    def tree(self) -> Node:
        return Node("program", 0, len(self._text), None,
                    [n for n in self._nodes if n is not None])

    def errors(self) -> List[ErrorInfo]:
        return collect_errors(self.tree())

    # ------------------------------------------------------------------ #

    def edit(self, start: int, end: int, ins: str = "") -> ReparseInfo:
        """Replace ``text[start:end]`` with ``ins`` and re-parse minimally."""
        old_text = self._text
        if not (0 <= start <= end <= len(old_text)):
            raise ValueError(f"invalid edit range [{start}, {end}) "
                             f"for text of length {len(old_text)}")
        new_text = old_text[:start] + ins + old_text[end:]
        delta = len(ins) - (end - start)
        old_spans, old_nodes = self._spans, self._nodes
        new_spans = line_spans(new_text)

        if not old_spans or not new_spans:
            self.reset(new_text)
            return self.last_reparse

        # Old lines [l0, l1] intersect the removed/replaced range.  The
        # newline terminating a line belongs to that line, so a deletion
        # whose end falls on a line boundary also merges (and therefore
        # invalidates) the following line: locate l1 with `end`, not
        # `end - 1`.
        l0 = _line_index_at(old_spans, start)
        l1 = _line_index_at(old_spans, end) if end > start else l0

        # In the new text those lines become new_spans[l0:mid_end]; the
        # remaining tail lines correspond 1:1 to old lines after l1.
        n_suffix = len(old_spans) - l1 - 1
        mid_end = len(new_spans) - n_suffix

        prefix = old_nodes[:l0]                      # untouched, spans cannot move
        middle = [parse_line(new_text, s, e) for s, e in new_spans[l0:mid_end]]
        suffix = [                                   # reused, shifted by THIS delta
            n.with_shift(delta) if n is not None else None
            for n in old_nodes[l1 + 1:]
        ]

        self._text = new_text
        self._spans = new_spans
        self._nodes = prefix + middle + suffix

        if mid_end > l0:
            r_start, r_end = new_spans[l0][0], new_spans[mid_end - 1][1]
        else:  # the edit only removed whole trailing lines
            point = new_spans[l0][0] if l0 < len(new_spans) else len(new_text)
            r_start = r_end = point
        self.last_reparse = ReparseInfo(r_start, r_end, max(0, mid_end - l0),
                                        len(new_spans), delta)
        return self.last_reparse

    def apply_edits(self, edits: Sequence[Tuple[int, int, str]]) -> List[ReparseInfo]:
        """Apply several non-overlapping edits (given in current-text
        coordinates) as one batch; returns one ReparseInfo per edit."""
        ordered = sorted(edits, key=lambda e: (e[0], e[1]), reverse=True)
        for a, b in zip(ordered, ordered[1:]):
            if a[0] < b[1]:
                raise ValueError("overlapping edits")
        return [self.edit(s, e, ins) for s, e, ins in ordered]
