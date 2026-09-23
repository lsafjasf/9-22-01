"""Regression tests for the fixed incremental parser.

Run:  python3 -m unittest test_incremental -v

Every test compares the incremental result against a from-scratch
``parse_full`` of the current text, node-by-node and position-by-position
(``tree_key`` covers kind, [start, end), value, errors and children).
"""
import random
import unittest

from parser_core import (
    apply_edit,
    collect_errors,
    first_mismatch,
    parse_full,
    tree_key,
)
from parser_fixed import IncrementalParser

DOC = (
    "let alpha = 1 + 2;\n"
    "# a comment in the middle\n"
    "let beta = alpha * 3;\n"
    "let gamma = (alpha + beta) * 2;\n"
    "42;\n"
)


def assert_matches_full(testcase, parser):
    mismatch = first_mismatch(parser.tree(), parse_full(parser.text))
    testcase.assertIsNone(mismatch, mismatch)
    testcase.assertEqual(
        [(e.message, e.start, e.end) for e in parser.errors()],
        [(e.message, e.start, e.end) for e in collect_errors(parse_full(parser.text))],
    )


class TestSymptomRegressions(unittest.TestCase):
    def test_paren_completion_keeps_positions(self):
        """Symptom 1: inserting '(' must not shift nodes before the edit."""
        p = IncrementalParser(DOC)
        before = [ (n.kind, n.start, n.end) for n in p.tree().children ]
        at = DOC.index("(alpha + beta") + 1
        p.edit(at, at, "(")                      # double the left paren
        after = [ (n.kind, n.start, n.end) for n in p.tree().children ]
        cut = DOC[:at].count("\n")              # lines fully before the edit
        self.assertEqual(before[:cut], after[:cut],
                         "nodes before the edit must keep their positions")
        assert_matches_full(self, p)
        close = p.text.index(";", at)
        p.edit(close, close, ")")
        assert_matches_full(self, p)

    def test_comment_add_and_delete(self):
        """Symptom 2: deleting a comment must not leave a stale node."""
        p = IncrementalParser("let a = 1;\nlet b = 2;\n")
        at = p.text.index("let b")
        p.edit(at, at, "# transient comment\n")
        assert_matches_full(self, p)
        self.assertEqual([n.kind for n in p.tree().children],
                         ["let", "comment", "let"])
        p.edit(at, at + len("# transient comment\n"), "")
        assert_matches_full(self, p)
        self.assertEqual([n.kind for n in p.tree().children], ["let", "let"])
        self.assertEqual(p.text, "let a = 1;\nlet b = 2;\n")

    def test_error_recovery_is_reproducible_and_not_sticky(self):
        """Symptom 3: a failed recovery must not poison later parses."""
        bad = "let = 3;\nlet ok = 1;\n"
        results = []
        for _ in range(3):                       # fresh parser every time
            p = IncrementalParser(bad)
            results.append((tree_key(p.tree()),
                            [(e.message, e.start, e.end) for e in p.errors()]))
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[1], results[2])
        self.assertTrue(results[0][1], "scenario requires errors")

        p = IncrementalParser(bad)
        p.edit(4, 4, "fixed ")                   # repair the document
        self.assertEqual(p.errors(), [])
        assert_matches_full(self, p)
        p.edit(4, 10, "broken again")            # break it differently
        assert_matches_full(self, p)
        again = IncrementalParser(p.text)        # same input, fresh parser
        self.assertEqual(tree_key(p.tree()), tree_key(again.tree()))


class TestEditSessions(unittest.TestCase):
    def test_multiple_simultaneous_edits(self):
        p = IncrementalParser(DOC)
        edits = [
            (DOC.index("1 + 2"), DOC.index("1 + 2") + len("1 + 2"), "7"),
            (DOC.index("alpha * 3"), DOC.index("alpha * 3") + len("3"), "4"),
            (DOC.index("42;"), DOC.index("42;") + len("42"), "(40 + 2)"),
        ]
        infos = p.apply_edits(edits)
        self.assertEqual(len(infos), 3)
        assert_matches_full(self, p)
        expected = DOC
        for s, e, ins in sorted(edits, reverse=True):
            expected = apply_edit(expected, s, e, ins)
        self.assertEqual(p.text, expected)

    def test_undo_redo(self):
        p = IncrementalParser(DOC)
        shadow = DOC
        history = []
        script = [
            (DOC.index("# a comment"), DOC.index("# a comment"), "#!"),
            (DOC.index("let gamma"), DOC.index("let gamma"), "let tmp = 9;\n"),
            (DOC.index("42;"), DOC.index("42;") + 3, "alpha - beta;"),
        ]
        for s, e, ins in script:                 # redo / forward
            removed = shadow[s:e]
            history.append((s, s + len(ins), removed))
            p.edit(s, e, ins)
            shadow = apply_edit(shadow, s, e, ins)
            self.assertEqual(p.text, shadow)
            assert_matches_full(self, p)
        for s, e, ins in reversed(history):      # undo / backward
            p.edit(s, e, ins)
            shadow = apply_edit(shadow, s, e, ins)
            self.assertEqual(p.text, shadow)
            assert_matches_full(self, p)
        self.assertEqual(p.text, DOC)
        self.assertEqual(tree_key(p.tree()), tree_key(parse_full(DOC)))

    def test_seeded_random_edit_fuzz(self):
        rng = random.Random(20260923)
        shadow = DOC
        p = IncrementalParser(shadow)
        alphabet = ["let x = 1;\n", "# c\n", "(", ")", ";", "a", "9", " + ", "\n", "="]
        for step in range(300):
            if rng.random() < 0.5 or not shadow:     # insert
                at = rng.randrange(len(shadow) + 1)
                ins = rng.choice(alphabet)
                p.edit(at, at, ins)
                shadow = apply_edit(shadow, at, at, ins)
            else:                                    # delete
                s = rng.randrange(len(shadow))
                e = min(len(shadow), s + rng.randrange(1, 12))
                p.edit(s, e, "")
                shadow = apply_edit(shadow, s, e, "")
            self.assertEqual(p.text, shadow, f"step {step}")
            mismatch = first_mismatch(p.tree(), parse_full(shadow))
            self.assertIsNone(mismatch, f"step {step}: {mismatch}")


class TestReparseRange(unittest.TestCase):
    def test_single_edit_reparses_only_affected_lines(self):
        big = "".join(f"let v{i} = {i} * 2 + (v0 + 1);\n" for i in range(400))
        p = IncrementalParser(big)
        at = big.index("v200 =") + len("v200 =")
        info = p.edit(at, at + 1, "7")           # change one digit mid-file
        print(f"\n  single-char edit in 400-line file -> {info}")
        self.assertLessEqual(info.lines_reparsed, 1)
        self.assertEqual(info.total_lines, 400)
        self.assertLess(info.end - info.start, len(big) // 10)
        assert_matches_full(self, p)

    def test_stats_reported_for_every_edit(self):
        p = IncrementalParser(DOC)
        session = [
            (DOC.index("1 + 2"), DOC.index("1 + 2") + 5, "(1 + 2)"),
            (None, None, None),                  # placeholder for comment delete
        ]
        info = p.edit(*session[0])
        print(f"  edit 1 -> {info}")
        self.assertGreater(info.lines_reparsed, 0)
        s = p.text.index("# a comment")
        e = p.text.index("\n", s) + 1
        info = p.edit(s, e, "")
        print(f"  edit 2 -> {info}")
        self.assertEqual(info.delta, -(e - s))
        assert_matches_full(self, p)


if __name__ == "__main__":
    unittest.main()
