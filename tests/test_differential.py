"""Differential tests: colstore engine vs naive row-based implementation."""

import os
import random
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from colstore import TableReader, TableWriter, pred
from colstore import engine, naive
from colstore.format import ENC_PLAIN, ENC_RLE


def compare_value(test, a, b):
    if a is None or b is None:
        test.assertEqual(a, b)
    elif isinstance(a, float) or isinstance(b, float):
        test.assertAlmostEqual(a, b, places=9)
    else:
        test.assertEqual(a, b)


def compare_rows(test, got, want):
    test.assertEqual(len(got), len(want), "row count differs")
    for ra, rb in zip(got, want):
        test.assertEqual(len(ra), len(rb))
        for x, y in zip(ra, rb):
            compare_value(test, x, y)


def compare_groups(test, got, want):
    test.assertEqual(len(got), len(want), "group count differs")
    for (ka, va), (kb, vb) in zip(got, want):
        test.assertEqual(len(ka), len(kb))
        for x, y in zip(ka, kb):
            compare_value(test, x, y)
        test.assertEqual(len(va), len(vb))
        for x, y in zip(va, vb):
            compare_value(test, x, y)


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="colstore_test_")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def build(self, schema, rows, block_rows=64, name="t"):
        path = os.path.join(self.dir, name)
        w = TableWriter(path, schema, block_rows=block_rows)
        w.extend(rows)
        w.close()
        return path


SCHEMA = [
    ("id", "int"),
    ("country", "str"),
    ("status", "str"),
    ("amount", "float"),
    ("qty", "int"),
]


def make_rows(seed, n):
    rng = random.Random(seed)
    countries = ["cn", "us", "de", "jp", "fr"]
    statuses = ["new", "paid", "shipped", "done"]
    rows = []
    for i in range(n):
        rows.append((
            i,
            rng.choice(countries) if rng.random() > 0.1 else None,
            rng.choice(statuses),
            round(rng.uniform(0, 1000), 2) if rng.random() > 0.15 else None,
            rng.randint(0, 100) if rng.random() > 0.05 else None,
        ))
    return rows


class TestScanAndFilter(Base):
    def test_random_predicates(self):
        rows = make_rows(1, 3000)
        path = self.build(SCHEMA, rows)
        reader = TableReader(path)
        queries = [
            [pred("country", "eq", "cn")],
            [pred("amount", "ge", 500.0), pred("qty", "lt", 10)],
            [pred("country", "isnull")],
            [pred("qty", "notnull"), pred("status", "ne", "new")],
            [pred("id", "lt", 100)],
            [pred("id", "gt", 10_000)],  # matches nothing
            [],
        ]
        for q in queries:
            cols = ["id", "amount"]
            info = {}
            got = []
            for data, idx, nrows in engine.iter_blocks(reader, cols, q, info):
                for r in (idx if idx is not None else range(nrows)):
                    got.append(tuple(data[c][r] for c in cols))
            want = naive.scan(SCHEMA, rows, cols, q)
            compare_rows(self, got, want)
        reader.close()

    def test_block_skipping_on_sorted_column(self):
        schema = [("k", "int"), ("v", "int")]
        rows = [(i, i * 7 % 13) for i in range(6400)]
        path = self.build(schema, rows, block_rows=64)
        reader = TableReader(path)
        info = {}
        list(engine.iter_blocks(reader, ["v"], [pred("k", "lt", 640)], info))
        self.assertGreater(info["blocks_skipped"], 80)
        full = sum(os.path.getsize(os.path.join(path, "col_%d.bin" % i)) for i in (0, 1))
        self.assertLess(reader.bytes_read, full // 2)
        reader.close()


class TestGroupBy(Base):
    def check(self, schema, rows, keys, aggs, predicates=(), budget=100000):
        path = self.build(schema, rows)
        reader = TableReader(path)
        got = engine.group_by(reader, keys, aggs, predicates, memory_budget=budget)
        want = naive.group_by(schema, rows, keys, aggs, predicates)
        compare_groups(self, got, want)
        reader.close()

    def test_basic(self):
        rows = make_rows(2, 5000)
        aggs = [("count", None), ("count", "amount"), ("sum", "amount"),
                ("min", "qty"), ("max", "qty"), ("avg", "amount")]
        self.check(SCHEMA, rows, ["country"], aggs)
        self.check(SCHEMA, rows, ["country", "status"], aggs)
        self.check(SCHEMA, rows, [], aggs)  # global aggregation
        self.check(SCHEMA, rows, ["status"], aggs,
                   predicates=[pred("qty", "ge", 50)])

    def test_spill_matches_no_spill(self):
        rows = make_rows(3, 5000)
        aggs = [("count", None), ("sum", "amount"), ("avg", "qty")]
        for budget in (1, 7, 100):
            self.check(SCHEMA, rows, ["country", "status"], aggs, budget=budget)
        # high-cardinality key forces many distinct groups through spills
        self.check(SCHEMA, rows, ["id"], aggs, budget=13)

    def test_all_null_keys(self):
        schema = [("g", "str"), ("v", "int")]
        rows = [(None, i) for i in range(500)]
        self.check(schema, rows, ["g"], [("count", None), ("sum", "v")], budget=3)


class TestSortAndDistinct(Base):
    def test_sort(self):
        rows = make_rows(4, 4000)
        path = self.build(SCHEMA, rows)
        reader = TableReader(path)
        for budget in (100000, 50, 1):
            got = engine.sort_rows(reader, ["country", "qty", "amount"],
                                   ["qty", "amount"], memory_budget=budget)
            want = naive.sort_rows(SCHEMA, rows, ["country", "qty", "amount"],
                                   ["qty", "amount"])
            compare_rows(self, got, want)
        got = engine.sort_rows(reader, ["id", "amount"], ["amount"],
                               [pred("qty", "lt", 20)], memory_budget=33)
        want = naive.sort_rows(SCHEMA, rows, ["id", "amount"], ["amount"],
                               [pred("qty", "lt", 20)])
        compare_rows(self, got, want)
        reader.close()

    def test_distinct(self):
        rows = make_rows(5, 4000)
        path = self.build(SCHEMA, rows)
        reader = TableReader(path)
        for budget in (100000, 2):
            got = engine.distinct(reader, ["country", "status"], memory_budget=budget)
            want = naive.distinct(SCHEMA, rows, ["country", "status"])
            compare_rows(self, got, want)
        got = engine.distinct(reader, ["country"], [pred("amount", "isnull")],
                              memory_budget=1)
        want = naive.distinct(SCHEMA, rows, ["country"], [pred("amount", "isnull")])
        compare_rows(self, got, want)
        reader.close()


class TestRandomAccess(Base):
    def test_point_and_range(self):
        rows = make_rows(6, 5000)
        path = self.build(SCHEMA, rows, block_rows=128)
        reader = TableReader(path)
        rng = random.Random(7)
        cols = ["country", "amount"]
        for _ in range(200):
            i = rng.randrange(len(rows))
            self.assertEqual(reader.point(i, cols), naive.point(SCHEMA, rows, i, cols))
        for _ in range(50):
            a = rng.randrange(len(rows))
            b = min(len(rows), a + rng.randrange(0, 300))
            got = list(reader.range_scan(a, b, cols))
            want = naive.range_scan(SCHEMA, rows, a, b, cols)
            compare_rows(self, got, want)
        # point lookup reads only a couple of blocks, not the whole column
        reader2 = TableReader(path)
        before = reader2.bytes_read
        reader2.point(2500, ["amount"])
        self.assertLess(reader2.bytes_read - before, 8192)
        reader2.close()
        reader.close()


class TestEdgeCases(Base):
    def test_empty_and_single_row(self):
        for rows in ([], [("x", 1, 2.5)]):
            schema = [("a", "str"), ("b", "int"), ("c", "float")]
            path = self.build(schema, rows)
            reader = TableReader(path)
            got = engine.group_by(reader, ["a"], [("count", None), ("sum", "b")])
            want = naive.group_by(schema, rows, ["a"], [("count", None), ("sum", "b")])
            compare_groups(self, got, want)
            got = engine.sort_rows(reader, ["a", "b"], ["b"], memory_budget=1)
            want = naive.sort_rows(schema, rows, ["a", "b"], ["b"])
            compare_rows(self, got, want)
            reader.close()

    def test_all_null_column(self):
        schema = [("a", "int"), ("b", "str")]
        rows = [(None, "s%d" % (i % 5)) for i in range(1000)]
        path = self.build(schema, rows)
        reader = TableReader(path)
        got = engine.group_by(reader, ["b"], [("count", "a"), ("sum", "a"), ("avg", "a")])
        want = naive.group_by(schema, rows, ["b"], [("count", "a"), ("sum", "a"), ("avg", "a")])
        compare_groups(self, got, want)
        info = {}
        list(engine.iter_blocks(reader, ["b"], [pred("a", "eq", 1)], info))
        self.assertEqual(info.get("blocks_read", 0), 0)  # all-NULL stats prune everything
        self.assertGreater(info["blocks_skipped"], 0)
        reader.close()

    def test_dictionary_bloat_falls_back_to_plain(self):
        schema = [("u", "str"), ("v", "int")]
        rows = [("user_%08d" % i, i) for i in range(2000)]
        path = self.build(schema, rows, block_rows=256)
        reader = TableReader(path)
        encs = {reader.block_stats("u", i)["enc"] for i in range(reader.num_blocks)}
        self.assertEqual(encs, {ENC_PLAIN})  # bloat => PLAIN wins on size
        got = engine.group_by(reader, ["u"], [("count", None)], memory_budget=16)
        want = naive.group_by(schema, rows, ["u"], [("count", None)])
        compare_groups(self, got, want)
        reader.close()

    def test_extreme_skew_uses_rle(self):
        schema = [("heavy", "int"), ("v", "int")]
        rng = random.Random(8)
        rows = [(1 if rng.random() < 0.999 else rng.randint(2, 9), i)
                for i in range(20000)]
        path = self.build(schema, rows, block_rows=512)
        reader = TableReader(path)
        encs = {reader.block_stats("heavy", i)["enc"] for i in range(reader.num_blocks)}
        self.assertEqual(encs, {ENC_RLE})
        col_size = os.path.getsize(os.path.join(path, "col_0.bin"))
        self.assertLess(col_size, 20000)  # ~8B/row uncompressed
        got = engine.group_by(reader, ["heavy"], [("count", None), ("sum", "v")],
                              memory_budget=2)
        want = naive.group_by(schema, rows, ["heavy"], [("count", None), ("sum", "v")])
        compare_groups(self, got, want)
        reader.close()

    def test_wide_table_reads_only_touched_columns(self):
        ncols = 120
        schema = [("c%d" % i, "int") for i in range(ncols)]
        rows = [tuple(i * ncols + j for j in range(ncols)) for i in range(2000)]
        path = self.build(schema, rows, block_rows=256)
        reader = TableReader(path)
        cols = ["c3", "c77"]
        info = {}
        got = []
        for data, idx, nrows in engine.iter_blocks(reader, cols, [pred("c3", "ge", 0)], info):
            for r in (idx if idx is not None else range(nrows)):
                got.append(tuple(data[c][r] for c in cols))
        want = naive.scan(schema, rows, cols, [pred("c3", "ge", 0)])
        compare_rows(self, got, want)
        total = sum(os.path.getsize(os.path.join(path, "col_%d.bin" % i))
                    for i in range(ncols))
        self.assertLess(reader.bytes_read, total // 20)
        reader.close()

    def test_block_boundary_sizes(self):
        schema = [("k", "int")]
        for n in (1, 63, 64, 65, 640, 641):
            rows = [(i,) for i in range(n)]
            path = self.build(schema, rows, block_rows=64, name="b%d" % n)
            reader = TableReader(path)
            got = engine.sort_rows(reader, ["k"], ["k"], memory_budget=7)
            self.assertEqual(got, [(i,) for i in range(n)])
            reader.close()


if __name__ == "__main__":
    unittest.main()
