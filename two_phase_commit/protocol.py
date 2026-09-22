"""Protocol messages exchanged between coordinator and participants.

Only six message kinds are needed for the full protocol:

PREPARE         coordinator -> participant : "please vote on txn"
VOTE_COMMIT     participant -> coordinator : "I am prepared"
VOTE_ABORT      participant -> coordinator : "no, abort"
COMMIT          coordinator -> participant : global decision: commit
ABORT           coordinator -> participant : global decision: abort
ACK_COMMIT      participant -> coordinator : participant applied commit
STATUS_QUERY    either -> other            : recovery: "what is the decision?"

All messages carry ``txn`` so a participant can keep several blocked
transactions at once and every handler is idempotent.
"""

from __future__ import annotations

import json

KINDS = frozenset(
    {
        "PREPARE",
        "VOTE_COMMIT",
        "VOTE_ABORT",
        "COMMIT",
        "ABORT",
        "ACK_COMMIT",
        "ACK_ABORT",
        "STATUS_QUERY",
        "DECISION_COMMIT",
        "DECISION_ABORT",
        "DECISION_UNKNOWN",
    }
)


class Msg:
    """An immutable-ish protocol message.

    ``src`` is filled in by the transport when the message is sent.
    """

    __slots__ = ("kind", "txn", "src", "dst")

    def __init__(self, kind, txn, src=None, dst=None):
        if kind not in KINDS:
            raise ValueError("unknown message kind: %r" % (kind,))
        self.kind = kind
        self.txn = txn
        self.src = src
        self.dst = dst

    def to_dict(self):
        return {"kind": self.kind, "txn": self.txn, "src": self.src, "dst": self.dst}

    @classmethod
    def from_dict(cls, data):
        return cls(data["kind"], data["txn"], data.get("src"), data.get("dst"))

    def to_json(self):
        return json.dumps(self.to_dict(), separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_json(cls, raw):
        return cls.from_dict(json.loads(raw))

    def __repr__(self):
        return "Msg(%s, txn=%r, src=%r, dst=%r)" % (
            self.kind,
            self.txn,
            self.src,
            self.dst,
        )

    def __eq__(self, other):
        if not isinstance(other, Msg):
            return NotImplemented
        return (self.kind, self.txn, self.src, self.dst) == (
            other.kind,
            other.txn,
            other.src,
            other.dst,
        )

    def __hash__(self):
        return hash((self.kind, self.txn, self.src, self.dst))
