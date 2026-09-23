"""Query execution engine: predicate pushdown, two-phase group-by / sort /
distinct with disk spilling when the memory budget is exceeded.

Phase 1 (partial): stream column blocks, prune blocks via statistics,
evaluate predicates, and build partial results bounded by `memory_budget`.
When the budget is exceeded, partial results are sorted and spilled to disk.
Phase 2 (merge): k-way merge the sorted spill files (plus the final
in-memory partial) into the final result.
"""

import heapq
import itertools
import os
import pickle
import struct
import tempfile

OPS = ("eq", "ne", "lt", "le", "gt", "ge", "isnull", "notnull")
AGG_FUNCS = ("count", "sum", "min", "max", "avg")


class Predicate(object):
    __slots__ = ("col", "op", "value")

    def __init__(self, col, op, value=None):
        if op not in OPS:
            raise ValueError("bad op %r" % op)
        self.col = col
        self.op = op
        self.value = value

    def __repr__(self):
        return "Predicate(%r, %r, %r)" % (self.col, self.op, self.value)


def pred(col, op, value=None):
    return Predicate(col, op, value)


def sort_key(v):
    """Total order with NULLs last."""
    return (v is None, v)


def _prunes(stats, p):
    """True if the block statistics prove no row can match the predicate."""
    if p.op == "isnull":
        return stats["nulls"] == 0
    if p.op == "notnull":
        return stats["nulls"] == stats["rows"]
    mn, mx = stats["min"], stats["max"]
    if mn is None:  # all values NULL: comparisons never match
        return True
    v = p.value
    if p.op == "eq":
        return v < mn or v > mx
    if p.op == "ne":
        return mn == mx == v
    if p.op == "lt":
        return mn >= v
    if p.op == "le":
        return mn > v
    if p.op == "gt":
        return mx <= v
    if p.op == "ge":
        return mx < v
    return False


def _match(v, p):
    if p.op == "isnull":
        return v is None
    if p.op == "notnull":
        return v is not None
    if v is None:
        return False
    x = p.value
    if p.op == "eq":
        return v == x
    if p.op == "ne":
        return v != x
    if p.op == "lt":
        return v < x
    if p.op == "le":
        return v <= x
    if p.op == "gt":
        return v > x
    if p.op == "ge":
        return v >= x
    raise ValueError(p.op)


def iter_blocks(reader, columns, predicates=(), info=None):
    """Yield (data, row_index_or_None, block_row_count) per non-skipped block.

    `data` maps every column in `columns` plus predicate columns to the
    decoded block values. `info` (optional dict) accumulates
    blocks_skipped / blocks_read / rows_matched.
    """
    predicates = list(predicates)
    need = list(dict.fromkeys(list(columns) + [p.col for p in predicates]))
    if not need:
        need = [reader.schema[0][0]]
    for i in range(reader.num_blocks):
        skip = False
        for p in predicates:
            if _prunes(reader.block_stats(p.col, i), p):
                skip = True
                break
        if skip:
            if info is not None:
                info["blocks_skipped"] = info.get("blocks_skipped", 0) + 1
            continue
        if info is not None:
            info["blocks_read"] = info.get("blocks_read", 0) + 1
        data = {c: reader.read_block(c, i) for c in need}
        nrows = len(data[need[0]])
        if predicates:
            idx = []
            for r in range(nrows):
                ok = True
                for p in predicates:
                    if not _match(data[p.col][r], p):
                        ok = False
                        break
                if ok:
                    idx.append(r)
        else:
            idx = None
        if info is not None:
            info["rows_matched"] = info.get("rows_matched", 0) + (len(idx) if idx is not None else nrows)
        yield data, idx, nrows


# ---------------------------------------------------------------- aggregates

def _agg_init(func):
    if func == "count":
        return 0
    if func == "avg":
        return (0, 0)  # (sum, count)
    return None  # sum / min / max: None means "no value yet"


def _agg_update(state, func, v):
    if func == "count":
        return state + (0 if v is None else 1)
    if v is None:
        return state
    if func == "sum":
        return v if state is None else state + v
    if func == "min":
        return v if state is None or v < state else state
    if func == "max":
        return v if state is None or v > state else state
    if func == "avg":
        return (state[0] + v, state[1] + 1)
    raise ValueError(func)


def _agg_combine(a, b, func):
    if func == "count":
        return a + b
    if func == "avg":
        return (a[0] + b[0], a[1] + b[1])
    if a is None:
        return b
    if b is None:
        return a
    if func == "sum":
        return a + b
    if func == "min":
        return a if a <= b else b
    if func == "max":
        return a if a >= b else b
    raise ValueError(func)


def _agg_finalize(state, func):
    if func == "avg":
        return (state[0] / state[1]) if state[1] else None
    return state


# ---------------------------------------------------------------- spilling

class _SpillSet(object):
    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="colstore_spill_")
        self.paths = []

    def write(self, items):
        path = os.path.join(self.dir, "run_%d.bin" % len(self.paths))
        with open(path, "wb") as f:
            for item in items:
                blob = pickle.dumps(item, protocol=4)
                f.write(struct.pack("<I", len(blob)))
                f.write(blob)
        self.paths.append(path)
        return path

    def cleanup(self):
        for p in self.paths:
            try:
                os.unlink(p)
            except OSError:
                pass
        try:
            os.rmdir(self.dir)
        except OSError:
            pass


def _iter_spill(path):
    with open(path, "rb") as f:
        while True:
            hdr = f.read(4)
            if not hdr:
                return
            (ln,) = struct.unpack("<I", hdr)
            yield pickle.loads(f.read(ln))


# ---------------------------------------------------------------- group by

def group_by(reader, keys, aggs, predicates=(), memory_budget=100000, info=None):
    """Two-phase GROUP BY.

    keys: list of column names; aggs: list of (func, col) with func in
    AGG_FUNCS and col None for count(*). Returns [(key_tuple, val_tuple)]
    sorted by key. Spills sorted partial aggregates to disk whenever the
    in-memory hash table grows beyond `memory_budget` groups.
    """
    for f, _ in aggs:
        if f not in AGG_FUNCS:
            raise ValueError("bad agg %r" % f)
    cols = list(keys) + [c for _, c in aggs if c is not None]
    spills = _SpillSet()
    nspills = 0
    table = {}
    try:
        for data, idx, nrows in iter_blocks(reader, cols, predicates, info):
            rows = idx if idx is not None else range(nrows)
            for r in rows:
                key = tuple(data[k][r] for k in keys)
                state = table.get(key)
                if state is None:
                    state = [_agg_init(f) for f, _ in aggs]
                    table[key] = state
                for j, (f, c) in enumerate(aggs):
                    v = 1 if c is None else data[c][r]
                    state[j] = _agg_update(state[j], f, v)
            if len(table) > memory_budget:
                spills.write(sorted(table.items(), key=lambda kv: tuple(sort_key(x) for x in kv[0])))
                nspills += 1
                table = {}
        if nspills:
            spills.write(sorted(table.items(), key=lambda kv: tuple(sort_key(x) for x in kv[0])))
            result = _merge_group_spills(spills.paths, aggs)
        else:
            result = [
                (k, tuple(_agg_finalize(s, f) for s, (f, _) in zip(st, aggs)))
                for k, st in table.items()
            ]
            result.sort(key=lambda kv: tuple(sort_key(x) for x in kv[0]))
        if info is not None:
            info["spills"] = info.get("spills", 0) + nspills
        return result
    finally:
        spills.cleanup()


def _merge_group_spills(paths, aggs):
    streams = [_iter_spill(p) for p in paths]
    merged = heapq.merge(*streams, key=lambda kv: tuple(sort_key(x) for x in kv[0]))
    out = []
    for key, group in itertools.groupby(merged, key=lambda kv: kv[0]):
        states = None
        for _, st in group:
            if states is None:
                states = list(st)
            else:
                states = [_agg_combine(a, b, f) for a, b, (f, _) in zip(states, st, aggs)]
        out.append((key, tuple(_agg_finalize(s, f) for s, (f, _) in zip(states, aggs))))
    return out


def distinct(reader, columns, predicates=(), memory_budget=100000, info=None):
    """DISTINCT over `columns`; returns sorted list of key tuples."""
    return [k for k, _ in group_by(reader, columns, [], predicates, memory_budget, info)]


# ---------------------------------------------------------------- sort

def sort_rows(reader, columns, order_by, predicates=(), memory_budget=50000, info=None):
    """External sort: sorted runs are spilled to disk when the in-memory
    buffer exceeds `memory_budget` rows, then k-way merged. Ascending order,
    NULLs last. Returns the sorted list of projected row tuples."""
    order_idx = [columns.index(c) for c in order_by]

    def keyf(row):
        return tuple(sort_key(row[i]) for i in order_idx)

    spills = _SpillSet()
    nspills = 0
    buf = []
    try:
        for data, idx, nrows in iter_blocks(reader, columns, predicates, info):
            rows = idx if idx is not None else range(nrows)
            for r in rows:
                buf.append(tuple(data[c][r] for c in columns))
            if len(buf) >= memory_budget:
                buf.sort(key=keyf)
                spills.write(buf)
                nspills += 1
                buf = []
        if nspills:
            if buf:
                buf.sort(key=keyf)
                spills.write(buf)
            result = list(heapq.merge(*[_iter_spill(p) for p in spills.paths], key=keyf))
        else:
            buf.sort(key=keyf)
            result = buf
        if info is not None:
            info["spills"] = info.get("spills", 0) + nspills
        return result
    finally:
        spills.cleanup()
