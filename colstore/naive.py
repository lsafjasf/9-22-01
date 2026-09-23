"""Naive row-based reference implementation used for differential testing.

A table is (schema, rows): schema is a list of (name, type), rows a list of
tuples. Everything is computed with straightforward full scans, no pruning,
no spilling. Aggregate semantics are shared with the engine on purpose so
the differential tests isolate scan / spill / merge correctness.
"""

from .engine import _agg_init, _agg_update, _agg_finalize, _match, sort_key


def _idx(schema, col):
    for i, (name, _) in enumerate(schema):
        if name == col:
            return i
    raise KeyError(col)


def filter_rows(schema, rows, predicates=()):
    pidx = [(_idx(schema, p.col), p) for p in predicates]
    out = []
    for row in rows:
        if all(_match(row[i], p) for i, p in pidx):
            out.append(row)
    return out


def project(schema, rows, columns):
    idx = [_idx(schema, c) for c in columns]
    return [tuple(row[i] for i in idx) for row in rows]


def scan(schema, rows, columns, predicates=()):
    return project(schema, filter_rows(schema, rows, predicates), columns)


def group_by(schema, rows, keys, aggs, predicates=()):
    kidx = [_idx(schema, k) for k in keys]
    aidx = [None if c is None else _idx(schema, c) for _, c in aggs]
    table = {}
    for row in filter_rows(schema, rows, predicates):
        key = tuple(row[i] for i in kidx)
        state = table.get(key)
        if state is None:
            state = [_agg_init(f) for f, _ in aggs]
            table[key] = state
        for j, (f, _) in enumerate(aggs):
            v = 1 if aidx[j] is None else row[aidx[j]]
            state[j] = _agg_update(state[j], f, v)
    out = [
        (k, tuple(_agg_finalize(s, f) for s, (f, _) in zip(st, aggs)))
        for k, st in table.items()
    ]
    out.sort(key=lambda kv: tuple(sort_key(x) for x in kv[0]))
    return out


def distinct(schema, rows, columns, predicates=()):
    return [k for k, _ in group_by(schema, rows, columns, [], predicates)]


def sort_rows(schema, rows, columns, order_by, predicates=()):
    proj = project(schema, filter_rows(schema, rows, predicates), columns)
    oidx = [columns.index(c) for c in order_by]
    proj.sort(key=lambda row: tuple(sort_key(row[i]) for i in oidx))
    return proj


def point(schema, rows, row_id, columns):
    idx = [_idx(schema, c) for c in columns]
    return tuple(rows[row_id][i] for i in idx)


def range_scan(schema, rows, start, stop, columns):
    idx = [_idx(schema, c) for c in columns]
    return [tuple(row[i] for i in idx) for row in rows[start:stop]]
