"""Durable append-only log with fsync. Every state machine transition is
persisted here BEFORE any message about it is sent."""
import json
import os


class LogCorruptError(Exception):
    """Raised when a log contains unreadable records. Automatic recovery
    must refuse to guess; use the read-only doctor tool instead."""


class LogStore:
    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    def append(self, record):
        line = json.dumps(record, sort_keys=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())

    def read_all(self):
        """Return (records, corrupt). corrupt is a list of
        (line_no, raw_text) for lines that failed to parse."""
        records, corrupt = [], []
        if not os.path.exists(self.path):
            return records, corrupt
        with open(self.path, "rb") as f:
            data = f.read()
        for i, raw in enumerate(data.splitlines()):
            if not raw.strip():
                continue
            try:
                records.append(json.loads(raw.decode("utf-8")))
            except (ValueError, UnicodeDecodeError):
                corrupt.append((i + 1, raw.decode("utf-8", errors="replace")))
        return records, corrupt

    def read_strict(self):
        records, corrupt = self.read_all()
        if corrupt:
            raise LogCorruptError(
                "%s: %d corrupt line(s), e.g. line %d: %r"
                % (self.path, len(corrupt), corrupt[0][0], corrupt[0][1][:80])
            )
        return records
