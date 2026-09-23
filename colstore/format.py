"""Columnar on-disk format.

Layout of a table directory:
    meta.json          table schema, block size, row count
    col_<i>.bin        one file per column

Layout of a column file:
    [4B magic "CSB1"]
    [block 0][block 1]...[block N-1]
    [json block index][4B index length][4B magic "CSIX"]

Layout of a block:
    [1B encoding][4B row_count][4B null_count][4B bitmap_len]
    [null bitmap (bit i set => row i is NULL)]
    [payload]

Payload encodings (only non-null values are stored, in row order):
    PLAIN: values packed back to back
    DICT:  [4B dict_size][1B code_width][dict values][codes]
    RLE:   [4B run_count][(value, 4B run_length) ...]

The per-block index keeps (offset, length, rows, nulls, encoding, min, max)
so queries can skip whole blocks and random access only decodes one block.
"""

import json
import os
import struct

ENC_PLAIN = 0
ENC_DICT = 1
ENC_RLE = 2
ENC_NAMES = {ENC_PLAIN: "PLAIN", ENC_DICT: "DICT", ENC_RLE: "RLE"}

TYPE_INT = "int"
TYPE_FLOAT = "float"
TYPE_STR = "str"
TYPES = (TYPE_INT, TYPE_FLOAT, TYPE_STR)

_FILE_MAGIC = b"CSB1"
_FOOTER_MAGIC = b"CSIX"
_BLOCK_HEADER = struct.Struct("<BIII")
_U32 = struct.Struct("<I")
_FOOTER = struct.Struct("<I4s")


def _pack_value(typ, v):
    if typ == TYPE_INT:
        return struct.pack("<q", v)
    if typ == TYPE_FLOAT:
        return struct.pack("<d", v)
    b = v.encode("utf-8")
    return _U32.pack(len(b)) + b


def _pack_values(typ, vals):
    if typ == TYPE_INT:
        return struct.pack("<%dq" % len(vals), *vals)
    if typ == TYPE_FLOAT:
        return struct.pack("<%dd" % len(vals), *vals)
    parts = []
    for v in vals:
        b = v.encode("utf-8")
        parts.append(_U32.pack(len(b)))
        parts.append(b)
    return b"".join(parts)


def _unpack_values(typ, buf, off, n):
    """Decode n packed values from buf starting at off; return (values, new_off)."""
    if n == 0:
        return [], off
    if typ == TYPE_INT:
        return list(struct.unpack_from("<%dq" % n, buf, off)), off + 8 * n
    if typ == TYPE_FLOAT:
        return list(struct.unpack_from("<%dd" % n, buf, off)), off + 8 * n
    out = []
    for _ in range(n):
        (ln,) = _U32.unpack_from(buf, off)
        off += 4
        out.append(buf[off:off + ln].decode("utf-8"))
        off += ln
    return out, off


def _encode_plain(typ, vals):
    return _pack_values(typ, vals)


def _encode_dict(typ, vals):
    dictionary = {}
    codes = []
    for v in vals:
        code = dictionary.get(v)
        if code is None:
            code = len(dictionary)
            dictionary[v] = code
        codes.append(code)
    items = [None] * len(dictionary)
    for v, c in dictionary.items():
        items[c] = v
    n = len(items)
    width = 1 if n <= 0xFF else (2 if n <= 0xFFFF else 4)
    fmt = {1: "<%dB", 2: "<%dH", 4: "<%dI"}[width] % len(codes)
    return struct.pack("<IB", n, width) + _pack_values(typ, items) + struct.pack(fmt, *codes)


def _encode_rle(typ, vals):
    runs = []
    run_val = vals[0]
    run_len = 1
    for v in vals[1:]:
        if v == run_val:
            run_len += 1
        else:
            runs.append((run_val, run_len))
            run_val, run_len = v, 1
    runs.append((run_val, run_len))
    parts = [_U32.pack(len(runs))]
    for v, ln in runs:
        parts.append(_pack_value(typ, v))
        parts.append(_U32.pack(ln))
    return b"".join(parts)


def _decode_payload(typ, enc, payload, nvals):
    if enc == ENC_PLAIN:
        vals, _ = _unpack_values(typ, payload, 0, nvals)
        return vals
    if enc == ENC_DICT:
        n, width = struct.unpack_from("<IB", payload, 0)
        items, off = _unpack_values(typ, payload, 5, n)
        fmt = {1: "<%dB", 2: "<%dH", 4: "<%dI"}[width] % nvals
        codes = struct.unpack_from(fmt, payload, off)
        return [items[c] for c in codes]
    if enc == ENC_RLE:
        (count,) = _U32.unpack_from(payload, 0)
        off = 4
        out = []
        for _ in range(count):
            vals, off = _unpack_values(typ, payload, off, 1)
            (ln,) = _U32.unpack_from(payload, off)
            off += 4
            out.extend(vals * ln)
        return out
    raise ValueError("unknown encoding %r" % enc)


class ColumnWriter(object):
    def __init__(self, path, typ, block_rows):
        self.path = path
        self.typ = typ
        self.block_rows = block_rows
        self.fd = open(path, "wb")
        self.fd.write(_FILE_MAGIC)
        self.index = []
        self.offset = len(_FILE_MAGIC)

    def append_block(self, values):
        row_count = len(values)
        null_count = 0
        non_null = []
        for v in values:
            if v is None:
                null_count += 1
            else:
                non_null.append(v)
        if null_count:
            bitmap = bytearray((row_count + 7) // 8)
            for i, v in enumerate(values):
                if v is None:
                    bitmap[i >> 3] |= 1 << (i & 7)
            bitmap = bytes(bitmap)
        else:
            bitmap = b""
        candidates = [(ENC_PLAIN, _encode_plain(self.typ, non_null))]
        if non_null:
            candidates.append((ENC_RLE, _encode_rle(self.typ, non_null)))
            candidates.append((ENC_DICT, _encode_dict(self.typ, non_null)))
        enc, payload = min(candidates, key=lambda c: len(c[1]))
        block = _BLOCK_HEADER.pack(enc, row_count, null_count, len(bitmap)) + bitmap + payload
        self.fd.write(block)
        self.index.append({
            "off": self.offset,
            "len": len(block),
            "rows": row_count,
            "nulls": null_count,
            "enc": enc,
            "min": min(non_null) if non_null else None,
            "max": max(non_null) if non_null else None,
        })
        self.offset += len(block)

    def close(self):
        idx = json.dumps(self.index).encode("utf-8")
        self.fd.write(idx)
        self.fd.write(_FOOTER.pack(len(idx), _FOOTER_MAGIC))
        self.fd.close()


class ColumnReader(object):
    def __init__(self, path, typ):
        self.path = path
        self.typ = typ
        self.fd = open(path, "rb")
        self.bytes_read = 0
        magic = self._read_at(0, 4)
        if magic != _FILE_MAGIC:
            raise ValueError("bad column file: %s" % path)
        self.fd.seek(-8, os.SEEK_END)
        idx_len, foot_magic = _FOOTER.unpack(self._read(8))
        if foot_magic != _FOOTER_MAGIC:
            raise ValueError("bad column footer: %s" % path)
        self.fd.seek(-8 - idx_len, os.SEEK_END)
        self.index = json.loads(self._read(idx_len).decode("utf-8"))
        self.num_blocks = len(self.index)
        self.row_offsets = []
        acc = 0
        for b in self.index:
            self.row_offsets.append(acc)
            acc += b["rows"]
        self.num_rows = acc

    def _read(self, n):
        data = self.fd.read(n)
        self.bytes_read += len(data)
        return data

    def _read_at(self, off, n):
        self.fd.seek(off)
        return self._read(n)

    def block_stats(self, i):
        return self.index[i]

    def read_block(self, i):
        meta = self.index[i]
        raw = self._read_at(meta["off"], meta["len"])
        enc, row_count, null_count, bitmap_len = _BLOCK_HEADER.unpack_from(raw, 0)
        pos = _BLOCK_HEADER.size + bitmap_len
        vals = _decode_payload(self.typ, enc, raw[pos:], row_count - null_count)
        if null_count:
            bitmap = raw[_BLOCK_HEADER.size:pos]
            out = []
            it = iter(vals)
            for r in range(row_count):
                if bitmap[r >> 3] >> (r & 7) & 1:
                    out.append(None)
                else:
                    out.append(next(it))
            return out
        return vals

    def close(self):
        self.fd.close()


class TableWriter(object):
    def __init__(self, path, schema, block_rows=4096):
        """schema: list of (column_name, type) with type in TYPES."""
        os.makedirs(path, exist_ok=True)
        self.path = path
        self.schema = list(schema)
        self.block_rows = block_rows
        self.writers = [
            ColumnWriter(os.path.join(path, "col_%d.bin" % i), typ, block_rows)
            for i, (_, typ) in enumerate(self.schema)
        ]
        self.buf = [[] for _ in self.schema]
        self.num_rows = 0

    def append(self, row):
        for i, v in enumerate(row):
            self.buf[i].append(v)
        self.num_rows += 1
        if len(self.buf[0]) >= self.block_rows:
            self._flush()

    def extend(self, rows):
        for row in rows:
            self.append(row)

    def _flush(self):
        if not self.buf or not self.buf[0]:
            return
        for i, w in enumerate(self.writers):
            w.append_block(self.buf[i])
        self.buf = [[] for _ in self.schema]

    def close(self):
        self._flush()
        for w in self.writers:
            w.close()
        meta = {
            "schema": [list(s) for s in self.schema],
            "block_rows": self.block_rows,
            "num_rows": self.num_rows,
        }
        with open(os.path.join(self.path, "meta.json"), "w") as f:
            json.dump(meta, f)


class TableReader(object):
    def __init__(self, path):
        self.path = path
        with open(os.path.join(path, "meta.json")) as f:
            meta = json.load(f)
        self.schema = [tuple(s) for s in meta["schema"]]
        self.block_rows = meta["block_rows"]
        self.num_rows = meta["num_rows"]
        self.col_index = {name: i for i, (name, _) in enumerate(self.schema)}
        self._readers = {}
        if self.schema:
            self.num_blocks = self.column(self.schema[0][0]).num_blocks
        else:
            self.num_blocks = 0

    def column(self, name):
        r = self._readers.get(name)
        if r is None:
            i = self.col_index[name]
            r = ColumnReader(os.path.join(self.path, "col_%d.bin" % i), self.schema[i][1])
            self._readers[name] = r
        return r

    @property
    def bytes_read(self):
        return sum(r.bytes_read for r in self._readers.values())

    def block_stats(self, col, i):
        return self.column(col).block_stats(i)

    def read_block(self, col, i):
        return self.column(col).read_block(i)

    def point(self, row_id, columns):
        """Random access to a single row; decodes only the covering block."""
        import bisect
        if not 0 <= row_id < self.num_rows:
            raise IndexError(row_id)
        base = self.column(self.schema[0][0])
        b = bisect.bisect_right(base.row_offsets, row_id) - 1
        local = row_id - base.row_offsets[b]
        return tuple(self.read_block(c, b)[local] for c in columns)

    def range_scan(self, start, stop, columns):
        """Random access to [start, stop); decodes only overlapping blocks."""
        import bisect
        start = max(0, start)
        stop = min(self.num_rows, stop)
        if start >= stop or not columns:
            return
        base = self.column(self.schema[0][0])
        b = bisect.bisect_right(base.row_offsets, start) - 1
        while start < stop and b < self.num_blocks:
            blocks = {c: self.read_block(c, b) for c in columns}
            n = len(blocks[columns[0]])
            lo = start - base.row_offsets[b]
            hi = min(n, stop - base.row_offsets[b])
            for r in range(lo, hi):
                yield tuple(blocks[c][r] for c in columns)
            start = base.row_offsets[b] + n
            b += 1

    def close(self):
        for r in self._readers.values():
            r.close()
        self._readers = {}
