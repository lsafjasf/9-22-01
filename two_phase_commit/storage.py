"""Durable append-only logs.

Every state transition that must survive a ``kill -9`` is written here
*before* the network is touched.  A log is a sequence of binary frames::

    magic(4=b'TPC1') | length(4 BE) | record_type(1) | payload | crc32(4 BE)

``crc32`` covers record_type + payload.  Each append is followed by
``flush()`` + ``os.fsync()``; the containing directory is fsync'd when the
log is created so that a rename/crash cannot orphan it.

A torn tail (power loss mid write) and a CRC mismatch both raise
:class:`LogCorruption`.  Callers must never silently truncate: the
forensics tool treats the prefix before the bad frame as the only
trustworthy evidence.
"""

from __future__ import annotations

import json
import os
import struct
import zlib

MAGIC = b"TPC1"
HEADER = struct.Struct(">4sIB")
FOOTER = struct.Struct(">I")
FRAME_OVERHEAD = HEADER.size + FOOTER.size

# Record types.  Records are JSON payloads so the forensics tool can read
# them with only the standard library.
RECORD_TYPES = {
    1: "BEGIN",       # coordinator: txn started
    2: "VOTE",        # participant: VOTE_COMMIT / VOTE_ABORT persisted
    3: "DECISION",    # coordinator: COMMIT / ABORT (commit point)
    4: "APPLY",       # participant: COMMIT applied / ABORT recorded
    5: "ACK",         # participant: ACK_COMMIT sent durability marker
    6: "END",         # coordinator: all acks received, txn finished
}
RECORD_CODES = {name: code for code, name in RECORD_TYPES.items()}


class LogCorruption(Exception):
    """Raised when a log frame is torn or fails its CRC check.

    ``offset`` is the byte offset of the bad frame; ``prefix_records`` are
    the intact records before it.
    """

    def __init__(self, message, offset=None, prefix_records=None):
        super().__init__(message)
        self.offset = offset
        self.prefix_records = prefix_records or []


class DurableLog:
    """Append-only fsync'd log of typed JSON records."""

    def __init__(self, path):
        self.path = path
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        new_file = not os.path.exists(path)
        self._fh = open(path, "r+b" if not new_file else "w+b")
        if new_file:
            self._fh.flush()
            _fsync_dir(directory)

    # -- writes -----------------------------------------------------------

    def append(self, record_type, payload):
        """Append one record, fsync, return its (offset, length).

        ``record_type`` may be the integer code or the record name.
        """

        if isinstance(record_type, str):
            record_type = RECORD_CODES[record_type]
        if record_type not in RECORD_TYPES:
            raise ValueError("bad record type %r" % (record_type,))
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        crc = zlib.crc32(struct.pack(">B", record_type) + body) & 0xFFFFFFFF
        frame = HEADER.pack(MAGIC, len(body), record_type) + body + FOOTER.pack(crc)
        offset = self._fh.seek(0, os.SEEK_END)
        self._fh.write(frame)
        self._fh.flush()
        os.fsync(self._fh.fileno())
        return offset, len(frame)

    def close(self):
        if not self._fh.closed:
            self._fh.flush()
            os.fsync(self._fh.fileno())
            self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    # -- reads ------------------------------------------------------------

    def read_all(self):
        """Return all intact records as ``(seq, type_name, payload, offset)``.

        Raises :class:`LogCorruption` at the first bad frame; the exception
        carries the readable prefix.
        """

        return read_log(self.path)

    def read_prefix(self):
        """Read the intact prefix, raising nothing.

        Returns ``(records, corruption_or_none)``.  Useful for recovery and
        for the forensics tool.
        """

        return read_log_prefix(self.path)


def _fsync_dir(directory):
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    fd = os.open(directory, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _iter_frames(path):
    with open(path, "rb") as fh:
        offset = 0
        while True:
            head = fh.read(HEADER.size)
            if not head:
                return
            if len(head) < HEADER.size:
                raise LogCorruption(
                    "truncated header at offset %d" % offset, offset=offset
                )
            magic, length, rtype = HEADER.unpack(head)
            if magic != MAGIC:
                raise LogCorruption(
                    "bad magic %r at offset %d" % (magic, offset), offset=offset
                )
            body = fh.read(length)
            footer = fh.read(FOOTER.size)
            frame_end = offset + FRAME_OVERHEAD + length
            if len(body) < length or len(footer) < FOOTER.size:
                raise LogCorruption(
                    "truncated frame at offset %d" % offset, offset=offset
                )
            (stored_crc,) = FOOTER.unpack(footer)
            actual_crc = zlib.crc32(struct.pack(">B", rtype) + body) & 0xFFFFFFFF
            if stored_crc != actual_crc:
                raise LogCorruption(
                    "crc mismatch at offset %d" % offset, offset=offset
                )
            if rtype not in RECORD_TYPES:
                raise LogCorruption(
                    "unknown record type %d at offset %d" % (rtype, offset),
                    offset=offset,
                )
            try:
                payload = json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise LogCorruption(
                    "unparseable record at offset %d: %s" % (offset, exc),
                    offset=offset,
                )
            yield offset, RECORD_TYPES[rtype], payload, frame_end
            offset = frame_end


def read_log(path):
    records = []
    gen = _iter_frames(path)
    try:
        for offset, rtype, payload, _end in gen:
            records.append((len(records), rtype, payload, offset))
    except LogCorruption as exc:
        exc.prefix_records = records
        raise
    return records


def read_log_prefix(path):
    """Read everything up to (but not including) a corrupt frame.

    Returns ``(records, corruption)`` where ``records`` entries are
    ``(seq, type_name, payload, offset)`` and corruption is ``None`` for a
    fully intact log.
    """

    records = []
    try:
        for offset, rtype, payload, _end in _iter_frames(path):
            records.append((len(records), rtype, payload, offset))
    except FileNotFoundError:
        return records, None
    except LogCorruption as exc:
        return records, exc
    return records, None


def corrupt_tail(path, mode="truncate", rng=None):
    """Test helper: damage the last frame to simulate a torn write.

    ``mode``:
      * ``"truncate"``  - cut the last frame mid-way
      * ``"zero"``      - overwrite payload bytes with zeros
      * ``"garbage"``   - overwrite the magic / CRC with random bytes
    """

    size = os.path.getsize(path)
    records = read_log(path)
    last_offset = records[-1][3]
    with open(path, "r+b") as fh:
        if mode == "truncate":
            cut = last_offset + FRAME_OVERHEAD + 1
            fh.truncate(min(cut, size - 1))
        elif mode == "zero":
            fh.seek(last_offset + HEADER.size)
            fh.write(b"\x00\x00\x00")
            fh.flush()
            os.fsync(fh.fileno())
        elif mode == "garbage":
            if rng is not None:
                bad = bytes(rng.randrange(0, 256) for _ in range(8))
            else:
                bad = b"\xde\xad\xbe\xef\xde\xad\xbe\xef"
            fh.seek(max(0, size - 8))
            fh.write(bad)
            fh.flush()
            os.fsync(fh.fileno())
        else:
            raise ValueError(mode)
