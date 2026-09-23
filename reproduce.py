"""Reproduce the three incremental-parser defects, then verify the fix.

Usage:  python3 reproduce.py

Exit code 0  <=> all three symptoms reproduce on parser_buggy AND none of
                 them reproduces on parser_fixed.
"""
import sys

from parser_core import collect_errors, first_mismatch, parse_full, trees_equal
from parser_buggy import BuggyIncrementalParser
from parser_fixed import IncrementalParser

DOC = (
    "let alpha = 1 + 2;\n"
    "# a comment in the middle\n"
    "let beta = alpha * 3;\n"
    "let gamma = alpha + beta;\n"
)


def scenario_1_paren_completion(make_parser):
    """Insert '(' then ')' around an expression; check positions after each."""
    p = make_parser(DOC)
    at = DOC.index("alpha + beta")          # insert '(' before the expression
    p.edit(at, at, "(")
    m1 = first_mismatch(p.tree(), parse_full(p.text))
    close_at = p.text.index("beta;") + len("beta")
    p.edit(close_at, close_at, ")")
    m2 = first_mismatch(p.tree(), parse_full(p.text))
    stats = getattr(p, "last_reparse", None)
    return m1 or m2, stats


def scenario_2_comment_deletion(make_parser):
    """Delete the comment line; the comment node must disappear."""
    p = make_parser(DOC)
    start = DOC.index("# a comment")
    end = DOC.index("\n", start) + 1        # remove the whole line incl. newline
    p.edit(start, end, "")
    mismatch = first_mismatch(p.tree(), parse_full(p.text))
    stale = any(n.kind == "comment" for n in p.tree().children)
    stats = getattr(p, "last_reparse", None)
    return mismatch or stale, stats


def scenario_3_error_recovery_poison(make_parser):
    """One recovery failure must not poison later parses of the same file."""
    bad = "let = 3;\nlet ok = 1;\n"
    p = make_parser(bad, doc_id="demo.pcl") if _accepts_doc_id(make_parser) else make_parser(bad)
    first_errors = [(e.message, e.start, e.end) for e in p.errors()]
    assert first_errors, "scenario requires an initial error"
    # Fix the document: give the let-statement a proper identifier.
    p.edit(4, 5, "fixed")
    second_errors = [(e.message, e.start, e.end) for e in p.errors()]
    mismatch = first_mismatch(p.tree(), parse_full(p.text))
    poisoned = second_errors == [(m, s + 0, t) for m, s, t in first_errors] and mismatch
    stats = getattr(p, "last_reparse", None)
    return (mismatch, poisoned), stats


def _accepts_doc_id(factory):
    return factory is BuggyIncrementalParser


def run(name, scenario, factory, expect_problem):
    result, stats = scenario(factory)
    problem = bool(result[0] if isinstance(result, tuple) else result)
    line = f"  [{name}] problem={'YES' if problem else 'no'}"
    if stats is not None:
        line += f"  last_reparse={stats}"
    ok = problem == expect_problem
    print(("PASS " if ok else "FAIL ") + line)
    return ok


def main():
    print("== buggy parser (expect all three symptoms) ==")
    ok = True
    ok &= run("S1 paren completion -> position drift", scenario_1_paren_completion,
              BuggyIncrementalParser, True)
    ok &= run("S2 comment deletion -> stale node", scenario_2_comment_deletion,
              BuggyIncrementalParser, True)
    ok &= run("S3 error recovery -> poisoned cache", scenario_3_error_recovery_poison,
              BuggyIncrementalParser, True)

    print("== fixed parser (expect none) ==")
    ok &= run("S1 paren completion", scenario_1_paren_completion,
              IncrementalParser, False)
    ok &= run("S2 comment deletion", scenario_2_comment_deletion,
              IncrementalParser, False)
    ok &= run("S3 error recovery", scenario_3_error_recovery_poison,
              IncrementalParser, False)

    print("== reparse-range statistics on the fixed parser ==")
    p = IncrementalParser(DOC)
    edits = [
        (DOC.index("alpha + beta"), DOC.index("alpha + beta"), "("),
        (None, None, ")"),          # filled in below
        (DOC.index("# a comment"), DOC.index("\n", DOC.index("# a comment")) + 1, ""),
    ]
    info = p.edit(*edits[0]); print(f"  edit 1 insert '('  -> {info}")
    close_at = p.text.index("beta;") + len("beta")
    info = p.edit(close_at, close_at, ")"); print(f"  edit 2 insert ')'  -> {info}")
    info = p.edit(*edits[2]); print(f"  edit 3 delete comment -> {info}")
    assert trees_equal(p.tree(), parse_full(p.text))

    print("ALL OK" if ok else "SYMPTOMS NOT REPRODUCED / FIX INCOMPLETE")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
