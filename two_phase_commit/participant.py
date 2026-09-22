"""Participant state machine for two-phase commit.

Per-transaction states
----------------------
(no state) / INIT  nothing durable known about the txn.
PREPARED          a VOTE_COMMIT record is fsync'd: resources are reserved
                  and the participant may no longer decide unilaterally.
                  Without a decision it stays blocked forever, polling the
                  coordinator with STATUS_QUERY.
COMMITTED         an APPLY(COMMIT) record is fsync'd; the commit effect
                  happened before the record was considered durable and
                  ACK_COMMIT is only sent afterwards.
ABORTED           an APPLY(ABORT) record is fsync'd (presumed-abort
                  friendly); ACK_ABORT is sent afterwards.

Idempotency
-----------
* duplicate COMMIT/ABORT simply re-send the ack; the APPLY record is the
  idempotency key guarding the real resource effect;
* duplicate PREPARE after a durable vote re-sends that vote;
* duplicate votes are deduplicated by the coordinator.

Durability moments
------------------
1. VOTE_COMMIT fsync'd *before* the VOTE_COMMIT message leaves the node.
2. APPLY(COMMIT) fsync'd before ACK_COMMIT is sent (the commit callback
   runs immediately before the fsync so "record durable" implies "effect
   happened"; recovery never re-invokes the effect).
3. APPLY(ABORT) fsync'd before ACK_ABORT.
"""

from __future__ import annotations

from .protocol import Msg
from .storage import DurableLog

DEFAULT_QUERY_RETRY = 0.15


class Participant:
    def __init__(
        self,
        node_id,
        log,
        coordinator_id="coordinator",
        vote_fn=None,
        commit_fn=None,
        abort_fn=None,
        query_retry=DEFAULT_QUERY_RETRY,
        prepared_hook=None,
        applied_hook=None,
    ):
        self.node_id = node_id
        self.log = log
        self.coordinator_id = coordinator_id
        self.vote_fn = vote_fn or (lambda txn: True)
        self.commit_fn = commit_fn or (lambda txn: None)
        self.abort_fn = abort_fn or (lambda txn: None)
        self.query_retry = query_retry
        # Test hooks: prepared_hook after VOTE_COMMIT fsync before the vote
        # send; applied_hook after APPLY fsync before the ack send.
        self.prepared_hook = prepared_hook
        self.applied_hook = applied_hook
        self._ctx = None
        self._txns = {}   # txn -> state dict
        self.conflicts = []  # impossible conflicting decisions, for tests

    def boot(self, ctx):
        self._ctx = ctx
        self._recover()

    def _recover(self):
        records, corruption = self.log.read_prefix()
        if corruption is not None:
            raise RuntimeError(
                "participant %s log corrupt at offset %s; use the forensics "
                "inspect tool; refusing to guess"
                % (self.node_id, corruption.offset)
            )
        for seq, rtype, payload, _off in records:
            txn = payload["txn"]
            st = self._txns.setdefault(txn, self._blank(txn))
            if rtype == "VOTE":
                st["vote"] = payload["vote"]
                if payload["vote"] == "VOTE_COMMIT":
                    st["state"] = "PREPARED"
            elif rtype == "APPLY":
                if payload["outcome"] == "COMMIT":
                    st["state"] = "COMMITTED"
                else:
                    st["state"] = "ABORTED"
            elif rtype == "ACK":
                st["acked"] = payload["outcome"]
        for txn, st in self._txns.items():
            if st["state"] == "PREPARED":
                self._arm_query(txn)
            elif st["state"] == "COMMITTED":
                self._send_ack(txn, "COMMIT")
            elif st["state"] == "ABORTED":
                self._send_ack(txn, "ABORT")

    @staticmethod
    def _blank(txn):
        return {
            "txn": txn,
            "state": "INIT",
            "vote": None,
            "query_timer": None,
            "acked": None,
        }

    # -- observability ----------------------------------------------------

    def blocked(self):
        """Prepared but no decision yet: must stay blocked."""

        return sorted(
            txn for txn, st in self._txns.items() if st["state"] == "PREPARED"
        )

    def state_of(self, txn):
        st = self._txns.get(txn)
        return None if st is None else st["state"]

    def committed_txns(self):
        return sorted(
            txn for txn, st in self._txns.items() if st["state"] == "COMMITTED"
        )

    def aborted_txns(self):
        return sorted(
            txn for txn, st in self._txns.items() if st["state"] == "ABORTED"
        )

    # -- messages ---------------------------------------------------------

    def handle(self, msg):
        {
            "PREPARE": self._on_prepare,
            "COMMIT": self._on_commit,
            "ABORT": self._on_abort,
            "DECISION_COMMIT": self._on_commit,
            "DECISION_ABORT": self._on_abort,
            "DECISION_UNKNOWN": self._on_unknown,
        }.get(msg.kind, lambda m: None)(msg)

    def on_timer(self, token):
        name, txn = token
        st = self._txns.get(txn)
        if name == "query" and st is not None and st["state"] == "PREPARED":
            st["query_timer"] = None
            self._ctx.send(Msg("STATUS_QUERY", txn, dst=self.coordinator_id))
            self._arm_query(txn)

    # -- phase 1 ----------------------------------------------------------

    def _on_prepare(self, msg):
        st = self._txns.setdefault(msg.txn, self._blank(msg.txn))
        if st["state"] == "COMMITTED":
            self._send_ack(msg.txn, "COMMIT")
            return
        if st["state"] == "ABORTED":
            self._send_ack(msg.txn, "ABORT")
            return
        if st["state"] == "PREPARED":
            # Duplicate PREPARE after a durable vote: repeat the vote.
            self._ctx.send(
                Msg(st["vote"], msg.txn, dst=self.coordinator_id)
            )
            return
        # First time we see this txn: evaluate the resource.
        try:
            yes = bool(self.vote_fn(msg.txn))
        except Exception:
            yes = False
        if yes:
            # Durable before the vote message is sent.
            self.log.append("VOTE", {"txn": msg.txn, "vote": "VOTE_COMMIT"})
            st["vote"] = "VOTE_COMMIT"
            st["state"] = "PREPARED"
            if self.prepared_hook is not None:
                self.prepared_hook(msg.txn)
            self._ctx.send(Msg("VOTE_COMMIT", msg.txn, dst=self.coordinator_id))
            self._arm_query(msg.txn)
        else:
            self.log.append("VOTE", {"txn": msg.txn, "vote": "VOTE_ABORT"})
            st["vote"] = "VOTE_ABORT"
            st["state"] = "ABORTED"
            self.log.append("APPLY", {"txn": msg.txn, "outcome": "ABORT"})
            try:
                self.abort_fn(msg.txn)
            except Exception:
                pass
            self._send_ack(msg.txn, "ABORT")

    # -- phase 2 ----------------------------------------------------------

    def _on_commit(self, msg):
        st = self._txns.setdefault(msg.txn, self._blank(msg.txn))
        if st["state"] == "ABORTED":
            self.conflicts.append((self._ctx.time, msg.txn, "COMMIT"))
            self._send_ack(msg.txn, "ABORT")
            return
        if st["state"] == "COMMITTED":
            self._send_ack(msg.txn, "COMMIT")  # duplicate: idempotent
            return
        if st["state"] != "PREPARED":
            # COMMIT without a local prepare record can only happen if this
            # node never voted; the global decision is authoritative, so
            # record a synthetic reservation before applying.
            self.log.append("VOTE", {"txn": msg.txn, "vote": "VOTE_COMMIT"})
            st["vote"] = "VOTE_COMMIT"
            st["state"] = "PREPARED"
        # Effect first, durable record immediately after; the record is the
        # idempotency key so the effect is never replayed after a crash.
        self.commit_fn(msg.txn)
        self.log.append("APPLY", {"txn": msg.txn, "outcome": "COMMIT"})
        st["state"] = "COMMITTED"
        if self.applied_hook is not None:
            self.applied_hook(msg.txn, "COMMIT")
        self._disarm_query(st)
        self._send_ack(msg.txn, "COMMIT")

    def _on_abort(self, msg):
        st = self._txns.setdefault(msg.txn, self._blank(msg.txn))
        if st["state"] == "COMMITTED":
            self.conflicts.append((self._ctx.time, msg.txn, "ABORT"))
            self._send_ack(msg.txn, "COMMIT")
            return
        if st["state"] == "ABORTED":
            self._send_ack(msg.txn, "ABORT")  # duplicate: idempotent
            return
        if st["state"] == "INIT":
            self.log.append("VOTE", {"txn": msg.txn, "vote": "VOTE_ABORT"})
            st["vote"] = "VOTE_ABORT"
        self.abort_fn(msg.txn)
        self.log.append("APPLY", {"txn": msg.txn, "outcome": "ABORT"})
        st["state"] = "ABORTED"
        if self.applied_hook is not None:
            self.applied_hook(msg.txn, "ABORT")
        self._disarm_query(st)
        self._send_ack(msg.txn, "ABORT")

    def _on_unknown(self, msg):
        # Coordinator has no durable decision yet; stay blocked and keep
        # polling.  (It must not abort locally after a YES vote.)
        st = self._txns.get(msg.txn)
        if st is not None and st["state"] == "PREPARED" and st["query_timer"] is None:
            self._arm_query(msg.txn)

    # -- helpers ----------------------------------------------------------

    def _arm_query(self, txn):
        st = self._txns[txn]
        if st["query_timer"] is None:
            st["query_timer"] = self._ctx.set_timer(
                self.query_retry, ("query", txn)
            )

    def _disarm_query(self, st):
        if st["query_timer"] is not None:
            self._ctx.cancel_timer(st["query_timer"])
            st["query_timer"] = None

    def _send_ack(self, txn, outcome):
        kind = "ACK_COMMIT" if outcome == "COMMIT" else "ACK_ABORT"
        st = self._txns.get(txn)
        if st is not None and st["acked"] != outcome:
            self.log.append("ACK", {"txn": txn, "outcome": outcome})
            st["acked"] = outcome
        self._ctx.send(Msg(kind, txn, dst=self.coordinator_id))
