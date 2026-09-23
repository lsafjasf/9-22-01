"""Tiny line-oriented language — shared lexer / parser / AST.

    program     := {line}
    line        := blank | comment | statement
    comment     := '#' <any chars to end of line>
    statement   := 'let' ident '=' expr ';' | expr ';'
    expr        := unary/binary ops  = - + * /  and ( )
    number      := [0-9]+
    ident       := [A-Za-z_][A-Za-z0-9_]*

Only the Python standard library is used.
Nodes carry absolute half-open character spans [start, end).
Error recovery is deterministic: the same input text always yields the
same tree and the same error list (no exceptions, no mutable caches).

This module is shared by the buggy and the fixed incremental parsers so
that differences in behaviour come only from the incremental driver.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple


# --------------------------------------------------------------------------- #
# Tokens
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Token:
    kind: str          # NUMBER | IDENT | KEYWORD | PUNCT | COMMENT
    text: str
    start: int
    end: int


_PUNCT_SINGLE = set("+-*/=();")


def _is_ident_start(ch: str) -> bool:
    return ch.isalpha() or ch == "_"


def _is_ident_part(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def tokenize_line(text: str, start: int, end: int) -> List[Token]:
    """Tokenize text[start:end] (a single physical line, no newline)."""
    tokens: List[Token] = []
    i = start
    while i < end:
        ch = text[i]
        if ch in " \t\r":
            i += 1
            continue
        if ch == "#":
            j = i + 1
            while j < end and text[j] != "\n":
                j += 1
            tokens.append(Token("COMMENT", text[i:j], i, j))
            i = j
            continue
        if ch.isdigit():
            j = i + 1
            while j < end and text[j].isdigit():
                j += 1
            tokens.append(Token("NUMBER", text[i:j], i, j))
            i = j
            continue
        if _is_ident_start(ch):
            j = i + 1
            while j < end and _is_ident_part(text[j]):
                j += 1
            word = text[i:j]
            kind = "KEYWORD" if word == "let" else "IDENT"
            tokens.append(Token(kind, word, i, j))
            i = j
            continue
        if ch in _PUNCT_SINGLE:
            tokens.append(Token("PUNCT", ch, i, i + 1))
            i += 1
            continue
        j = i + 1
        while j < end and text[j] not in " \t\r\n" and text[j] not in _PUNCT_SINGLE \
                and not text[j].isalnum() and text[j] != "_" and text[j] != "#":
            j += 1
        tokens.append(Token("PUNCT", text[i:j], i, j))
        i = j
    return tokens


# --------------------------------------------------------------------------- #
# AST
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ErrorInfo:
    message: str
    start: int
    end: int

    def shift(self, delta: int) -> "ErrorInfo":
        return ErrorInfo(self.message, self.start + delta, self.end + delta)


@dataclass
class Node:
    kind: str
    start: int
    end: int
    value: object = None
    children: List["Node"] = field(default_factory=list)
    errors: List[ErrorInfo] = field(default_factory=list)

    def with_shift(self, delta: int) -> "Node":
        """A deep copy of this subtree with every span moved by ``delta``."""
        return Node(
            kind=self.kind,
            start=self.start + delta,
            end=self.end + delta,
            value=self.value,
            children=[c.with_shift(delta) for c in self.children],
            errors=[e.shift(delta) for e in self.errors],
        )


# --------------------------------------------------------------------------- #
# Line spans
# --------------------------------------------------------------------------- #

def line_spans(text: str) -> List[Tuple[int, int]]:
    """Half-open spans of every physical line, excluding '\\n'.

    A trailing empty line is not reported (it contains no characters).
    """
    spans: List[Tuple[int, int]] = []
    start = 0
    n = len(text)
    i = 0
    while i < n:
        if text[i] == "\n":
            spans.append((start, i))
            start = i + 1
        i += 1
    if start < n:
        spans.append((start, n))
    return spans


def _line_index_at(spans: List[Tuple[int, int]], pos: int) -> int:
    """Index of the line that contains position ``pos``.

    A position at the end of the text is mapped to the last line.
    """
    lo, hi = 0, len(spans)
    while lo < hi:
        mid = (lo + hi) // 2
        if spans[mid][1] < pos:
            lo = mid + 1
        else:
            hi = mid
    if lo >= len(spans):
        lo = len(spans) - 1
    return lo


# --------------------------------------------------------------------------- #
# Parser (one line / whole document)
# --------------------------------------------------------------------------- #

_BINARY_PREC = {"=": 1, "+": 2, "-": 2, "*": 3, "/": 3}
_UNARY = set("+-")


class _LineParser:
    def __init__(self, text: str, start: int, end: int):
        self.text = text
        self.line_start = start
        self.line_end = end
        self.tokens = tokenize_line(text, start, end)
        self.pos = 0

    def peek(self, offset: int = 0) -> Optional[Token]:
        idx = self.pos + offset
        return self.tokens[idx] if idx < len(self.tokens) else None

    def take(self) -> Optional[Token]:
        tok = self.peek()
        if tok is not None:
            self.pos += 1
        return tok

    def expect_punct(self, ch: str) -> Optional[Token]:
        tok = self.peek()
        if tok is not None and tok.kind == "PUNCT" and tok.text == ch:
            self.pos += 1
            return tok
        return None

    # -- top-level line ---------------------------------------------------- #

    def parse_line(self) -> Optional[Node]:
        tok = self.peek()
        if tok is None:
            return None
        if tok.kind == "COMMENT":
            comment = self.take()
            return Node("comment", comment.start, comment.end, comment.text)
        return self.parse_statement()

    def parse_statement(self) -> Node:
        first = self.peek()
        start = first.start
        errors: List[ErrorInfo] = []
        if first.kind == "KEYWORD" and first.text == "let":
            self.take()
            name = self.peek()
            if name is not None and name.kind == "IDENT":
                self.take()
                ident = Node("ident", name.start, name.end, name.text)
            else:
                at = name
                ident = Node("error", at.start if at else start,
                             at.end if at else start)
                errors.append(ErrorInfo("expected identifier in let",
                                        at.start if at else start,
                                        at.end if at else start))
            eq = self.expect_punct("=")
            if eq is None:
                at = self.peek()
                errors.append(ErrorInfo("expected '=' in let",
                                        at.start if at else start,
                                        at.end if at else start))
            expr = self.parse_expression(errors)
            semi = self.expect_punct(";")
            if semi is None:
                at = self.peek()
                errors.append(ErrorInfo("expected ';'",
                                        at.start if at else start,
                                        at.end if at else start))
            end = self.text_end(start)
            node = Node("let", start, end, None, [ident, expr], errors)
        else:
            expr = self.parse_expression(errors)
            semi = self.expect_punct(";")
            if semi is None:
                at = self.peek()
                errors.append(ErrorInfo("expected ';'",
                                        at.start if at else start,
                                        at.end if at else start))
            end = self.text_end(start)
            node = Node("expr_stmt", start, end, None, [expr], errors)

        tok = self.peek()
        while tok is not None:
            if tok.kind != "COMMENT":
                errors.append(ErrorInfo("unexpected token after ';'",
                                        tok.start, tok.end))
            self.take()
            tok = self.peek()
        return node

    def text_end(self, fallback: int) -> int:
        if self.tokens:
            return self.tokens[-1].end
        return fallback

    # -- expressions ------------------------------------------------------- #

    def parse_expression(self, errors: List[ErrorInfo], min_prec: int = 1) -> Node:
        left = self.parse_unary(errors)
        while True:
            tok = self.peek()
            if tok is None or tok.kind != "PUNCT" or tok.text not in _BINARY_PREC:
                break
            prec = _BINARY_PREC[tok.text]
            if prec < min_prec:
                break
            self.take()
            right = self.parse_expression(errors, prec + 1)
            left = Node("binary", left.start, right.end, tok.text, [left, right])
        return left

    def parse_unary(self, errors: List[ErrorInfo]) -> Node:
        tok = self.peek()
        if tok is not None and tok.kind == "PUNCT" and tok.text in _UNARY:
            self.take()
            operand = self.parse_unary(errors)
            return Node("unary", tok.start, operand.end, tok.text, [operand])
        return self.parse_atom(errors)

    def parse_atom(self, errors: List[ErrorInfo]) -> Node:
        tok = self.peek()
        if tok is None:
            pos = self.line_end
            errors.append(ErrorInfo("expected expression", pos, pos))
            return Node("error", pos, pos)
        if tok.kind == "NUMBER":
            self.take()
            return Node("number", tok.start, tok.end, tok.text)
        if tok.kind == "IDENT":
            self.take()
            return Node("ident", tok.start, tok.end, tok.text)
        if tok.kind == "KEYWORD":
            self.take()
            errors.append(ErrorInfo(f"unexpected keyword '{tok.text}'", tok.start, tok.end))
            return Node("error", tok.start, tok.end, tok.text)
        if tok.kind == "COMMENT":
            self.take()
            errors.append(ErrorInfo("unexpected comment", tok.start, tok.end))
            return Node("error", tok.start, tok.end, tok.text)
        if tok.text == "(":
            self.take()
            inner = self.parse_expression(errors)
            close = self.expect_punct(")")
            if close is None:
                errors.append(ErrorInfo("unmatched '('", tok.start, tok.end))
                at = self.peek()
                end = at.start if at is not None else inner.end
                return Node("paren", tok.start, end, None, [inner])
            return Node("paren", tok.start, close.end, None, [inner])
        if tok.text == ")":
            self.take()
            errors.append(ErrorInfo("unmatched ')'", tok.start, tok.end))
            return Node("error", tok.start, tok.end, tok.text)
        self.take()
        errors.append(ErrorInfo(f"unexpected token '{tok.text}'", tok.start, tok.end))
        return Node("error", tok.start, tok.end, tok.text)


def parse_line(text: str, start: int, end: int) -> Optional[Node]:
    return _LineParser(text, start, end).parse_line()


def parse_full(text: str) -> Node:
    """Parse the whole document from scratch."""
    items: List[Node] = []
    for lstart, lend in line_spans(text):
        node = parse_line(text, lstart, lend)
        if node is not None:
            items.append(node)
    return Node("program", 0, len(text), None, items)


def collect_errors(root: Node) -> List[ErrorInfo]:
    out: List[ErrorInfo] = []

    def walk(node: Node) -> None:
        out.extend(node.errors)
        for child in node.children:
            walk(child)

    walk(root)
    out.sort(key=lambda e: (e.start, e.end, e.message))
    return out


# --------------------------------------------------------------------------- #
# Structural comparison (node-by-node, position-by-position)
# --------------------------------------------------------------------------- #

def tree_key(node: Optional[Node]):
    """A hashable, fully structural key: kind, span, value, errors, children."""
    if node is None:
        return None
    return (
        node.kind,
        node.start,
        node.end,
        node.value,
        tuple((e.message, e.start, e.end) for e in node.errors),
        tuple(tree_key(c) for c in node.children),
    )


def trees_equal(a: Optional[Node], b: Optional[Node]) -> bool:
    return tree_key(a) == tree_key(b)


def first_mismatch(a: Optional[Node], b: Optional[Node], path: str = "program") -> Optional[str]:
    """Human-readable description of the first structural difference."""
    if (a is None) != (b is None):
        return f"{path}: one side is None (a={a is None}, b={b is None})"
    if a is None:
        return None
    for attr in ("kind", "start", "end", "value"):
        va, vb = getattr(a, attr), getattr(b, attr)
        if va != vb:
            return f"{path}.{attr}: {va!r} != {vb!r}"
    ea = [(e.message, e.start, e.end) for e in a.errors]
    eb = [(e.message, e.start, e.end) for e in b.errors]
    if ea != eb:
        return f"{path}.errors: {ea!r} != {eb!r}"
    if len(a.children) != len(b.children):
        return (f"{path}: child count {len(a.children)} != {len(b.children)} "
                f"({[c.kind for c in a.children]} vs {[c.kind for c in b.children]})")
    for i, (ca, cb) in enumerate(zip(a.children, b.children)):
        r = first_mismatch(ca, cb, f"{path}/{ca.kind}[{i}]")
        if r is not None:
            return r
    return None


def apply_edit(text: str, start: int, end: int, ins: str) -> str:
    return text[:start] + ins + text[end:]
