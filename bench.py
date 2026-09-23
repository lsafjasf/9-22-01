"""Performance & memory benchmark for colstore.

Run:  python3 bench.py
"""

import os
import random
import shutil
import sys
import tempfile
import time
import tracemalloc

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from colstore import TableReader, TableWriter, pred
from colstore import engine, naive


def fmt_bytes(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f%s" % (n, unit) if unit != "B" else "%dB" % n
        n /= 1024.0


def dir_size(path):
    return sum(os.path.getsize(os.path.join(path, f)) for f in os.listdir(path))


def timed(fn):
    tracemalloc.start()
    t0 = time.perf_counter()
    out = fn()
    dt = time.perf_counter() - t0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return out, dt, peak


def build(path, schema, rows, block_rows=8192):
    t0 = time.perf_counter()
    w = TableWriter(path, schema, block_rows=block_rows)
    w.extend(rows)
    w.close()
    return time.perf_counter() - t0


def main():
    base = tempfile.mkdtemp(prefix="colstore_bench_")
    print("bench dir: %s\n" % base)
    rng = random.Random(42)

    # ------------------------------------------------ dataset 1: analytics
    N = 300_000
    schema = [("ts", "int"), ("country", "str"), ("status", "str"),
              ("amount", "float"), ("user_id", "int"), ("score", "float")]
    countries = ["cn", "us", "de", "jp", "fr", "br", "in", "gb"]
    statuses = ["new", "paid", "shipped", "done"]
    rows = []
    st = "new"
    for i in range(N):
        if rng.random() < 0.002:
            st = rng.choice(statuses)
        rows.append((
            1_700_000_000 + i * 10 + rng.randint(0, 9),   # sorted-ish ts
            rng.choice(countries),
            st,                                            # long runs -> RLE
            round(rng.uniform(1, 5000), 2),
            rng.randint(0, 200_000),
            round(rng.uniform(0, 100), 1) if rng.random() > 0.3 else None,
        ))
    wt = build(os.path.join(base, "analytics"), schema, rows)
    print("== analytics: %d rows x %d cols, write %.2fs, on-disk %s ==" %
          (N, len(schema), wt, fmt_bytes(dir_size(os.path.join(base, "analytics")))))

    # Q1: selective range + equality, predicate pushdown
    def q1():
        r = TableReader(os.path.join(base, "analytics"))
        info = {}
        out = []
        for data, idx, nr in engine.iter_blocks(
                r, ["amount"], [pred("ts", "ge", 1_700_000_000 + 290_000 * 10),
                                pred("country", "eq", "cn")], info):
            out.extend(data["amount"][i] for i in (idx if idx is not None else range(nr)))
        res = (len(out), info, r.bytes_read)
        r.close()
        return res
    (n1, info1, b1), dt, peak = timed(q1)
    print("Q1 filter ts>=tail & country='cn': %d rows, %.3fs, peak %s, "
          "blocks read %d / skipped %d, bytes read %s" %
          (n1, dt, fmt_bytes(peak), info1["blocks_read"],
           info1["blocks_skipped"], fmt_bytes(b1)))

    def q1_naive():
        return len(naive.scan(schema, rows, ["amount"],
                              [pred("ts", "ge", 1_700_000_000 + 290_000 * 10),
                               pred("country", "eq", "cn")]))
    n1n, dtn, peakn = timed(q1_naive)
    print("   naive row scan:               %d rows, %.3fs, peak %s" %
          (n1n, dtn, fmt_bytes(peakn)))

    # Q2: group by with forced spilling (tiny memory budget)
    aggs = [("count", None), ("sum", "amount"), ("avg", "score")]
    def q2(budget):
        r = TableReader(os.path.join(base, "analytics"))
        info = {}
        res = engine.group_by(r, ["country", "status"], aggs,
                              memory_budget=budget, info=info)
        r.close()
        return res, info
    (g2, info2), dt, peak = timed(lambda: q2(100000))
    print("Q2 group by country,status (no spill):  %d groups, %.3fs, peak %s" %
          (len(g2), dt, fmt_bytes(peak)))
    (g2s, info2s), dt, peak = timed(lambda: q2(8))
    print("Q2 group by country,status (budget=8):  %d groups, %.3fs, peak %s, spills %d" %
          (len(g2s), dt, fmt_bytes(peak), info2s["spills"]))
    assert [k for k, _ in g2] == [k for k, _ in g2s]

    # Q3: high-cardinality group by with spilling
    def q3(budget):
        r = TableReader(os.path.join(base, "analytics"))
        info = {}
        res = engine.group_by(r, ["user_id"], [("count", None), ("sum", "amount")],
                              memory_budget=budget, info=info)
        r.close()
        return res, info
    (g3, info3), dt, peak = timed(lambda: q3(10_000_000))
    print("Q3 group by user_id (no spill):     %d groups, %.3fs, peak %s" %
          (len(g3), dt, fmt_bytes(peak)))
    (g3s, info3s), dt, peak = timed(lambda: q3(2000))
    print("Q3 group by user_id (budget=2000):  %d groups, %.3fs, peak %s, spills %d" %
          (len(g3s), dt, fmt_bytes(peak), info3s["spills"]))
    assert [k for k, _ in g3] == [k for k, _ in g3s]

    # Q4: external sort
    def q4(budget):
        r = TableReader(os.path.join(base, "analytics"))
        info = {}
        res = engine.sort_rows(r, ["country", "amount"], ["amount"],
                               memory_budget=budget, info=info)
        r.close()
        return res, info
    (s4, info4), dt, peak = timed(lambda: q4(10_000_000))
    print("Q4 sort by amount (in memory):   %d rows, %.3fs, peak %s" %
          (len(s4), dt, fmt_bytes(peak)))
    (s4s, info4s), dt, peak = timed(lambda: q4(20_000))
    print("Q4 sort by amount (budget=20k):  %d rows, %.3fs, peak %s, spills %d" %
          (len(s4s), dt, fmt_bytes(peak), info4s["spills"]))
    assert s4 == s4s

    # Q5: point lookup + range scan random access
    def q5():
        r = TableReader(os.path.join(base, "analytics"))
        t0 = time.perf_counter()
        for i in range(0, N, 997):
            r.point(i, ["country", "amount"])
        pt = (time.perf_counter() - t0) / (N // 997)
        t0 = time.perf_counter()
        n = sum(1 for _ in r.range_scan(150_000, 150_500, ["ts", "amount"]))
        rt = time.perf_counter() - t0
        b = r.bytes_read
        r.close()
        return pt, rt, n, b
    (pt, rt, n5, b5), dt, peak = timed(q5)
    print("Q5 point lookup: %.2f us/row; range scan of %d rows: %.3fs, bytes read %s" %
          (pt * 1e6, n5, rt, fmt_bytes(b5)))

    # --------------------------------------------- dataset 2: extreme skew
    rows2 = [(1 if rng.random() < 0.999 else rng.randint(2, 9), i) for i in range(N)]
    build(os.path.join(base, "skewed"), [("heavy", "int"), ("v", "int")], rows2)
    sz = dir_size(os.path.join(base, "skewed"))
    print("\n== skewed: 99.9%% same value, on-disk %s (raw would be %s) ==" %
          (fmt_bytes(sz), fmt_bytes(N * 2 * 8)))
    def q6():
        r = TableReader(os.path.join(base, "skewed"))
        res = engine.group_by(r, ["heavy"], [("count", None)])
        r.close()
        return res
    g6, dt, peak = timed(q6)
    print("Q6 group by heavy: %d groups, %.3fs, peak %s" % (len(g6), dt, fmt_bytes(peak)))

    # ------------------------------------------ dataset 3: dictionary bloat
    M = 100_000
    rows3 = [("user_%012d" % i, i) for i in range(M)]
    build(os.path.join(base, "bloat"), [("u", "str"), ("v", "int")], rows3)
    print("\n== dict bloat: %d unique strings, on-disk %s ==" %
          (M, fmt_bytes(dir_size(os.path.join(base, "bloat")))))
    def q7():
        r = TableReader(os.path.join(base, "bloat"))
        info = {}
        out = []
        for data, idx, nr in engine.iter_blocks(
                r, ["v"], [pred("u", "eq", "user_%012d" % 77_777)], info):
            out.extend(data["v"][i] for i in (idx if idx is not None else range(nr)))
        res = (out, info, r.bytes_read)
        r.close()
        return res
    (o7, info7, b7), dt, peak = timed(q7)
    print("Q7 point predicate on unique string: %s, %.3fs, blocks read %d / skipped %d, "
          "bytes read %s" % (o7, dt, info7["blocks_read"], info7["blocks_skipped"],
                             fmt_bytes(b7)))

    # ------------------------------------------------ dataset 4: wide table
    W, WN = 200, 50_000
    wschema = [("c%d" % i, "int") for i in range(W)]
    rows4 = [tuple(rng.randint(0, 1000) for _ in range(W)) for _ in range(WN)]
    build(os.path.join(base, "wide"), wschema, rows4, block_rows=4096)
    total = dir_size(os.path.join(base, "wide"))
    print("\n== wide: %d rows x %d cols, on-disk %s ==" % (WN, W, fmt_bytes(total)))
    def q8():
        r = TableReader(os.path.join(base, "wide"))
        info = {}
        res = engine.group_by(r, ["c7"], [("sum", "c199")],
                              [pred("c7", "lt", 50)], info=info)
        b = r.bytes_read
        r.close()
        return res, info, b
    (g8, info8, b8), dt, peak = timed(q8)
    print("Q8 group by c7 (2 of %d cols): %d groups, %.3fs, peak %s, bytes read %s "
          "(%.1f%% of table)" % (W, len(g8), dt, fmt_bytes(peak), fmt_bytes(b8),
                                 100.0 * b8 / total))

    # --------------------------------------------- dataset 5: null-heavy
    rows5 = [(None if rng.random() < 0.4 else rng.randint(0, 100),
              None if rng.random() < 0.4 else round(rng.uniform(0, 1), 3))
             for _ in range(N)]
    build(os.path.join(base, "nulls"), [("a", "int"), ("b", "float")], rows5)
    def q9():
        r = TableReader(os.path.join(base, "nulls"))
        res = engine.group_by(r, ["a"], [("count", None), ("avg", "b")],
                              [pred("a", "notnull")], memory_budget=500)
        r.close()
        return res
    g9, dt, peak = timed(q9)
    print("\n== null-heavy: 40%% NULLs ==\nQ9 group by a (notnull): %d groups, %.3fs, peak %s"
          % (len(g9), dt, fmt_bytes(peak)))

    shutil.rmtree(base, True)
    print("\ndone.")


if __name__ == "__main__":
    main()
