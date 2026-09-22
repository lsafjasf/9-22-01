"""Coordinator state machine for two-phase commit.

Per-transaction states
----------------------
``WAIT``    PREPARE sent, collecting votes (volatile until any vote is
            observed, but the txn itself is durable from BEGIN).
``COMMIT``  all votes were YES and the COMMIT decision record is durable.
            COMMIT messages are (re)sent until every participant acks.
``ABORT``   a NO vote, a timeout, or duplicate prepare-txn handling
            produced a durable ABORT decision; ABORT is (re)sent until
            every participant acks.
``DONE``    all acks received; an END record is appended.

Durability order (the heart of the safety argument)
---------------------------------------------------
1. BEGIN is fsync'd before PREPARE is sent.
2. DECISION(COMMIT/ABORT) is fsync'd **before** the corresponding
   COMMIT/ABORT message is sent to anyone.  A crash at any point can
   therefore only erase messages, never an unpublished decision.
3. Every participant fsyncs VOTE_COMMIT before replying and fsyncs
   APPLY(COMMIT) before exposing/ack'ing the commit (see participant.py).

Recovery
--------
On boot the log is replayed:

* no DECISION record for a txn  -> presumed abort: persist ABORT now
  (it is still safe: no COMMIT decision could ever have existed), then
  drive phase 2;
* DECISION present               -> resend that decision idempotently;
* END present                   -> nothing to do.

Because there is exactly one coordinator with stable identity, a
participant that is blocked after a vote can always resolve itself by
asking STATUS_QUERY; the coordinator answers from durable state.
"""

from __future__ import annotations

from .protocol import Msg
from .storage import DurableLog

# Virtual-time timer tokens (seconds of simulator clock).
DEFAULT_PREPARE_TIMEOUT = 1.0
DEFAULT_PREPARE_RETRY = 0.25
DEFAULT_DECISION_RETRY = 0.25
DEFAULT_STATUS_RETRY = 0.15
MAX_DECISION_RETRIES = 60  # bounded phase-2 retry; queries remain available


class Coordinator:
    def __init__(
        self,
        node_id,
        log,
        participants,
        prepare_timeout=DEFAULT_PREPARE_TIMEOUT,
        prepare_retry=DEFAULT_PREPARE_RETRY,
        decision_retry=DEFAULT_DECISION_RETRY,
        max_retries=MAX_DECISION_RETRIES,
        decision_hook=None,
    ):
        self.node_id = node_id
        self.log = log
        self.participants = tuple(participants)
        self.prepare_timeout = prepare_timeout
        self.prepare_retry = prepare_retry
        self.decision_retry = decision_retry
        self.max_retries = max_retries
        # Test/demo hook fired after the DECISION fsync and strictly before
        # any decision message is sent.  Killing the node here proves the
        # durability-before-send ordering.
        self.decision_hook = decision_hook
        self._ctx = None
        self._txns = {}  # txn -> state dict

    # -- lifecycle --------------------------------------------------------

    def boot(self, ctx):
        self._ctx = ctx
        self._recover()

    def _recover(self):
        records, corruption = self.log.read_prefix()
        if corruption is not None:
            # Never guess.  Refuse to run; the forensics CLI is the manual
            # path for this situation.
            raise RuntimeError(
                "coordinator log corrupt at offset %s; use the forensics "
                "inspect tool and repair manually before restart"
                % (corruption.offset,)
            )
        for seq, rtype, payload, _off in records:
            txn = payload["txn"]
            st = self._txns.setdefault(txn, self._blank_txn(txn))
            st["seq"] = seq
            if rtype == "BEGIN":
                st["participants"] = tuple(payload["participants"])
            elif rtype == "DECISION":
                st["state"] = payload["decision"]
                st["decided_seq"] = seq
            elif rtype == "END":
                st["state"] = "DONE"
                st["ended"] = True
                st["final_decision"] = payload.get(
                    "decision", st.get("final_decision")
                )
        # Resume every unfinished transaction.
        for txn, st in self._txns.items():
            if st["state"] == "WAIT":
                # Presumed abort: no decision could have been sent before
                # the crash, because sends happen strictly after the
                # durable DECISION record.
                self._persist_decision(txn, "ABORT", reason="recover-wait")
            if st["state"] in ("COMMIT", "ABORT") and not st["ended"]:
                st["retry_timer"] = self._ctx.set_timer(
                    self.decision_retry, ("decision-retry", txn)
                )

    @staticmethod
    def _blank_txn(txn):
        return {
            "txn": txn,
            "state": "WAIT",
            "participants": (),
            "votes": {},          # participant -> "VOTE_COMMIT"/"VOTE_ABORT"
            "acks_commit": set(),
            "acks_abort": set(),
            "prepare_timer": None,
            "prepare_retry_timer": None,
            "retry_timer": None,
            "retries": 0,
            "decided_seq": None,
            "ended": False,
            "final_decision": None,
            "seq": -1,
        }

    # -- public API -------------------------------------------------------

    def begin_txn(self, txn, participants=None):
        """Persist BEGIN then broadcast PREPARE. Idempotent by txn id."""

        if txn in self._txns:
            return
        parts = tuple(participants if participants is not None else self.participants)
        self.log.append("BEGIN", {"txn": txn, "participants": list(parts)})
        st = self._blank_txn(txn)
        st["participants"] = parts
        self._txns[txn] = st
        self._broadcast_prepare(txn)
        st["prepare_timer"] = self._ctx.set_timer(
            self.prepare_timeout, ("prepare-timeout", txn)
        )
        st["prepare_retry_timer"] = self._ctx.set_timer(
            self.prepare_retry, ("prepare-retry", txn)
        )

    def blocked(self):
        """Txns still missing a decision (observability for operators)."""

        return sorted(
            txn for txn, st in self._txns.items() if st["state"] == "WAIT"
        )

    def open_txns(self):
        """Txns decided but not yet fully acknowledged."""

        return sorted(
            txn
            for txn, st in self._txns.items()
            if st["state"] in ("COMMIT", "ABORT") and not st["ended"]
        )

    def state_of(self, txn):
        st = self._txns.get(txn)
        return None if st is None else st["state"]

    # -- message handling -------------------------------------------------

    def handle(self, msg):
        handler = {
            "VOTE_COMMIT": self._on_vote,
            "VOTE_ABORT": self._on_vote,
            "ACK_COMMIT": self._on_ack,
            "ACK_ABORT": self._on_ack,
            "STATUS_QUERY": self._on_status_query,
        }.get(msg.kind)
        if handler is not None:
            handler(msg)

    def on_timer(self, token):
        name, txn = token[0], token[1]
        st = self._txns.get(txn)
        if st is None:
            return
        if name == "prepare-timeout" and st["state"] == "WAIT":
            self._persist_decision(txn, "ABORT", reason="prepare-timeout")
        elif name == "prepare-retry" and st["state"] == "WAIT":
            st["prepare_retry_timer"] = None
            self._retry_prepare(txn)
            st["prepare_retry_timer"] = self._ctx.set_timer(
                self.prepare_retry, ("prepare-retry", txn)
            )
        elif name == "decision-retry" and st["state"] in ("COMMIT", "ABORT"):
            st["retry_timer"] = None
            if st["ended"]:
                return
            self._drive_decision(txn)
            if st["retries"] < self.max_retries:
                st["retries"] += 1
                st["retry_timer"] = self._ctx.set_timer(
                    self.decision_retry, ("decision-retry", txn)
                )

    # -- phase 1 ----------------------------------------------------------

    def _broadcast_prepare(self, txn):
        for pid in self._txns[txn]["participants"]:
            self._ctx.send(Msg("PREPARE", txn, dst=pid))

    def _retry_prepare(self, txn):
        # Resend only to participants whose vote has not been observed;
        # duplicate PREPARE is answered by a duplicate durable vote.
        st = self._txns[txn]
        for pid in st["participants"]:
            if pid not in st["votes"]:
                self._ctx.send(Msg("PREPARE", txn, dst=pid))

    def _on_vote(self, msg):
        st = self._txns.get(msg.txn)
        if st is None or st["state"] == "DONE":
            return
        if st["state"] != "WAIT":
            # Decision already made: let the sender recover it.  Sending
            # the decision here is a harmless extra idempotent nudge.
            self._send_decision(msg.txn, msg.src, st["state"])
            return
        st["votes"][msg.src] = msg.kind
        if msg.kind == "VOTE_ABORT":
            self._persist_decision(msg.txn, "ABORT", reason="vote-abort")
            return
        if len(st["votes"]) == len(st["participants"]) and all(
            v == "VOTE_COMMIT" for v in st["votes"].values()
        ):
            self._persist_decision(msg.txn, "COMMIT", reason="unanimous")

    # -- decisions --------------------------------------------------------

    def _persist_decision(self, txn, decision, reason):
        st = self._txns[txn]
        if st["state"] != "WAIT":
            return
        _off, length = self.log.append(
            "DECISION", {"txn": txn, "decision": decision, "reason": reason}
        )
        st["state"] = decision
        st["decided_at"] = self._ctx.time
        if self.decision_hook is not None:
            self.decision_hook(txn, decision)
        if st["prepare_timer"] is not None:
            self._ctx.cancel_timer(st["prepare_timer"])
            st["prepare_timer"] = None
        if st["prepare_retry_timer"] is not None:
            self._ctx.cancel_timer(st["prepare_retry_timer"])
            st["prepare_retry_timer"] = None
        # Durability-before-send: the record is fsync'd before this point.
        self._drive_decision(txn)
        st["retry_timer"] = self._ctx.set_timer(
            self.decision_retry, ("decision-retry", txn)
        )

    def _drive_decision(self, txn):
        st = self._txns[txn]
        kind = st["state"]  # "COMMIT" or "ABORT"
        acked = st["acks_commit"] if kind == "COMMIT" else st["acks_abort"]
        for pid in st["participants"]:
            if pid not in acked:
                self._send_decision(txn, pid, kind)

    def _send_decision(self, txn, pid, kind):
        self._ctx.send(Msg(kind, txn, dst=pid))

    def _on_ack(self, msg):
        st = self._txns.get(msg.txn)
        if st is None:
            return
        if msg.kind == "ACK_COMMIT" and st["state"] == "COMMIT":
            st["acks_commit"].add(msg.src)
            if len(st["acks_commit"]) == len(st["participants"]):
                self._finish(msg.txn)
        elif msg.kind == "ACK_ABORT" and st["state"] == "ABORT":
            st["acks_abort"].add(msg.src)
            if len(st["acks_abort"]) == len(st["participants"]):
                self._finish(msg.txn)
        elif st["state"] in ("COMMIT", "ABORT"):
            # Late ack of the wrong kind: re-assert the real decision.
            self._send_decision(msg.txn, msg.src, st["state"])

    def _finish(self, txn):
        st = self._txns[txn]
        if st["ended"]:
            return
        decision = st["state"]
        self.log.append("END", {"txn": txn, "decision": decision})
        st["ended"] = True
        st["final_decision"] = decision
        st["state"] = "DONE"
        if st["retry_timer"] is not None:
            self._ctx.cancel_timer(st["retry_timer"])
            st["retry_timer"] = None

    # -- recovery protocol ------------------------------------------------

    def _on_status_query(self, msg):
        st = self._txns.get(msg.txn)
        if st is None or st["state"] == "WAIT":
            reply = "DECISION_UNKNOWN"
        elif st["state"] in ("COMMIT", "ABORT"):
            reply = "DECISION_" + st["state"]
        else:  # DONE
            reply = "DECISION_" + st["final_decision"]
        self._ctx.send(Msg(reply, msg.txn, dst=msg.src))
