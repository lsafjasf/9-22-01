"""Two-phase commit participant.

State machine per transaction:
  (none) --PREPARE--> PREPARED --DECISION commit--> COMMITTED
                        PREPARED --DECISION abort--> ABORTED
  PREPARED is a blocking state: the participant holds the transaction
  open (blocked()) until a decision arrives. COMMIT/ABORT are idempotent.

Persistence points (all fsync'd BEFORE any dependent message is sent):
  - "prepared" : the vote, BEFORE replying VOTE to the coordinator
  - "decision" : the outcome, BEFORE applying it and BEFORE the ACK
  - "conflict" : a contradictory decision (should never happen); the
                 conflicting command is NOT applied and NOT acked
"""
import os

from .logstore import LogStore


class Participant:
    def __init__(self, name, dir, coordinator="coord", vote="yes",
                 query_interval=2.0):
        self.name = name
        self.store = LogStore(os.path.join(dir, name + ".log"))
        self.coordinator = coordinator
        self.vote_policy = vote
        self.query_interval = query_interval
        self.alive = False
        self.next_timer_at = None
        self.txns = {}  # txn -> {"state": prepared|committed|aborted, "vote": ...}

    # -- lifecycle -------------------------------------------------------
    def start(self, sim):
        sim.register(self)
        self.alive = True
        self._replay()
        self._arm_timer(sim)

    def crash(self):
        self.alive = False
        self.txns = {}
        self.next_timer_at = None

    def restart(self, sim):
        self.alive = True
        self._replay()
        self._arm_timer(sim)

    def _replay(self):
        self.txns = {}
        for r in self.store.read_strict():
            if r["op"] == "prepared":
                self.txns[r["txn"]] = {"state": "prepared", "vote": r["vote"]}
            elif r["op"] == "decision":
                cur = self.txns.setdefault(r["txn"], {"state": None, "vote": None})
                cur["state"] = "committed" if r["decision"] == "commit" else "aborted"

    # -- queries ---------------------------------------------------------
    def blocked(self):
        """Transactions stuck in PREPARED with no decision received."""
        return {t: c["vote"] for t, c in self.txns.items() if c["state"] == "prepared"}

    def decision_of(self, txn):
        cur = self.txns.get(txn)
        if cur is None or cur["state"] == "prepared":
            return None
        return "commit" if cur["state"] == "committed" else "abort"

    # -- message handling --------------------------------------------------
    def on_message(self, msg, sim):
        t = msg["type"]
        txn = msg["txn"]
        if t == "PREPARE":
            cur = self.txns.get(txn)
            if cur is None:
                vote = self.vote_policy(txn) if callable(self.vote_policy) \
                    else self.vote_policy
                # persist the vote BEFORE replying
                self.store.append({"op": "prepared", "txn": txn, "vote": vote})
                self.txns[txn] = {"state": "prepared", "vote": vote}
                sim.send(self.name, msg["src"],
                         {"type": "VOTE", "txn": txn, "vote": vote})
            elif cur["state"] == "prepared":
                # duplicate PREPARE: resend the same vote
                sim.send(self.name, msg["src"],
                         {"type": "VOTE", "txn": txn, "vote": cur["vote"]})
            else:
                sim.send(self.name, msg["src"],
                         {"type": "ACK", "txn": txn,
                          "decision": self.decision_of(txn)})
        elif t == "DECISION":
            self._apply_decision(sim, msg["src"], txn, msg["decision"])
        elif t == "STATUS":
            if msg.get("decision"):
                self._apply_decision(sim, msg["src"], txn, msg["decision"])
        self._arm_timer(sim)

    def on_timer(self, sim):
        # blocked participants actively ask the coordinator for the outcome
        for txn, cur in self.txns.items():
            if cur["state"] == "prepared":
                sim.send(self.name, self.coordinator, {"type": "QUERY", "txn": txn})
        self._arm_timer(sim)

    # -- internals ---------------------------------------------------------
    def _apply_decision(self, sim, src, txn, decision):
        cur = self.txns.get(txn)
        new_state = "committed" if decision == "commit" else "aborted"
        if cur is not None and cur["state"] in ("committed", "aborted"):
            if cur["state"] != new_state:
                # contradictory decision: record it, refuse to apply
                self.store.append({"op": "conflict", "txn": txn,
                                   "have": cur["state"], "got": decision})
                return
            # duplicate decision: idempotent, just ack again
            sim.send(self.name, src, {"type": "ACK", "txn": txn, "decision": decision})
            return
        # persist the decision BEFORE applying it and BEFORE acking
        self.store.append({"op": "decision", "txn": txn, "decision": decision})
        if cur is None:
            self.txns[txn] = {"state": new_state, "vote": None}
        else:
            cur["state"] = new_state
        sim.send(self.name, src, {"type": "ACK", "txn": txn, "decision": decision})

    def _arm_timer(self, sim):
        if any(c["state"] == "prepared" for c in self.txns.values()):
            self.next_timer_at = sim.time + self.query_interval
        else:
            self.next_timer_at = None
