"""Buggy incremental parser — kept ONLY to reproduce the three defects.

It reuses the old syntax tree after an edit and reparses just the
affected lines, but it contains three real bugs (marked BUG 1/2/3):

  BUG 1  position drift: a cumulative shift accumulator is applied to
         *every* reused node (even nodes before the edit), and it is
         re-applied on top of already-shifted nodes, so after inserting
         a '(' the positions of the whole file drift.

  BUG 2  reuse boundary: old lines are classified as "before"/"after"
         the edit using only the edit *start* with strict comparisons,
         so a node that overlaps the removed range (e.g. a deleted
         comment) survives as a stale node.

  BUG 3  cache pollution: a module-level dict caches the result of a
         parse that needed error recovery, keyed by document id.  Once
         a document has produced errors, every later parse of the same
         document returns that same stale error result forever.

Do not use this module for anything except reproduce.py.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from parser_core import (
    ErrorInfo,
    Node,
    _line_index_at,
    collect_errors,
    line_spans,
    parse_line,
)

# BUG 3: global, mutable, never-invalidated cache of "failed" parses.
_POISON_CACHE: Dict[str, List[Optional[Node]]] = {}


def _nodes_have_errors(nodes: List[Optional[Node]]) -> bool:
    def walk(n: Node) -> bool:
        if n.errors:
            return True
        return any(walk(c) for c in n.children)

    return any(n is not None and walk(n) for n in nodes)


class BuggyIncrementalParser:
    def __init__(self, text: str = "", doc_id: str = "untitled"):
        self.doc_id = doc_id
        self._shift = 0          # BUG 1: cumulative shift accumulator
        self._text = ""
        self._spans: List[Tuple[int, int]] = []
        self._nodes: List[Optional[Node]] = []
        self.reset(text)

    # ------------------------------------------------------------------ #

    def reset(self, text: str) -> None:
        self._text = text
        self._spans = line_spans(text)
        self._nodes = [parse_line(text, s, e) for s, e in self._spans]
        self._apply_poison()

    def _apply_poison(self) -> None:
        # BUG 3: once a document needed error recovery, the cached error
        # result is replayed for every subsequent parse of that document.
        if self.doc_id in _POISON_CACHE:
            self._nodes = _POISON_CACHE[self.doc_id]
        elif _nodes_have_errors(self._nodes):
            _POISON_CACHE[self.doc_id] = self._nodes

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

    def edit(self, start: int, end: int, ins: str = "") -> None:
        old_text = self._text
        new_text = old_text[:start] + ins + old_text[end:]
        delta = len(ins) - (end - start)
        old_nodes = self._nodes
        old_spans = self._spans
        new_spans = line_spans(new_text)
        if not old_spans or not new_spans:
            self.reset(new_text)
            return

        # BUG 1: accumulate the delta and apply the *accumulated* shift
        # to every reused node — including nodes before the edit and
        # nodes that were already shifted by previous edits.
        self._shift += delta

        # Reparse the lines touched by the edit in the new text.
        l0 = _line_index_at(new_spans, start)
        ins_end = start + len(ins)
        l1_new = _line_index_at(new_spans, max(start, ins_end - 1)) if ins else l0
        middle = [parse_line(new_text, s, e) for s, e in new_spans[l0:l1_new + 1]]

        # BUG 2: classify old lines using only the edit start with
        # strict comparisons.  A line whose span merely touches the edit
        # (end == start) is dropped, and a line that overlaps the
        # *removed* range but starts at/after `start` (e.g. a deleted
        # comment) is kept and shifted — a stale node survives.
        prefix = [
            n.with_shift(self._shift) if n is not None else None
            for (s, e), n in zip(old_spans, old_nodes) if e < start
        ]
        suffix = [
            n.with_shift(self._shift) if n is not None else None
            for (s, e), n in zip(old_spans, old_nodes) if s >= start
        ]

        self._text = new_text
        self._spans = new_spans
        self._nodes = prefix + middle + suffix
        self._apply_poison()
