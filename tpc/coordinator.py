"""Two-phase commit coordinator.

State machine per transaction:
  (none) --begin--> PREPARING --all yes--> COMMITTING --all ack--> DONE
                   PREPARING --any no / timeout--> ABORTING --all ack--> DONE

Persistence points (all fsync'd BEFORE any dependent message is sent):
  - "begin"   : before PREPARE messages
  - "vote"    : as each VOTE arrives, before it can influence a decision
  - "decide"  : the decision record, BEFORE any DECISION message
  - "ack"     : as each ACK arrives
  - "done"    : once all participants acknowledged

Recovery: replay the log. If a "decide" record exists, resend the decision
to participants that have not acked. If no decision exists, presume abort
(safe: no participant can have committed without a persisted decision)
and persist DECIDE ABORT before notifying anyone.
"""
import os

from .logstore import LogStore


class _Txn:
    def __init__(self, parts):
        self.parts = list(parts)
        self.votes = {}
        self.decision = None
        self.acks = set()
        self.done = False
        self.deadline = None      # volatile, vote timeout
        self.next_resend = None   # volatile, decision resend timer
        self.next_prepare = None  # volatile, PREPARE retransmit timer


class Coordinator:
    def __init__(self, name, dir, vote_timeout=5.0, resend_interval=1.0):
        self.name = name
        self.store = LogStore(os.path.join(dir, name + ".log"))
        self.vote_timeout = vote_timeout
        self.resend_interval = resend_interval
        self.alive = False
        self.next_timer_at = None
        self.txns = {}

    # -- lifecycle -----------------------------------------------------
    def start(self, sim):
        sim.register(self)
        self.alive = True
        self._replay(sim)

    def crash(self):
        """Simulate kill -9: volatile state is lost, log survives."""
        self.alive = False
        self.txns = {}
        self.next_timer_at = None

    def restart(self, sim):
        self.alive = True
        self._replay(sim)

    def _replay(self, sim):
        self.txns = {}
        for r in self.store.read_strict():  # corrupt log -> refuse to guess
            op = r["op"]
            if op == "begin":
                self.txns[r["txn"]] = _Txn(r["parts"])
            elif op == "vote":
                self.txns[r["txn"]].votes[r["from"]] = r["vote"]
            elif op == "decide":
                self.txns[r["txn"]].decision = r["decision"]
            elif op == "ack":
                self.txns[r["txn"]].acks.add(r["from"])
            elif op == "done":
                self.txns[r["txn"]].done = True
        for txn, st in self.txns.items():
            if st.done:
                continue
            if st.decision is None:
                # presume abort: no persisted decision => no participant
                # can have committed
                self._decide(sim, txn, "abort")
            else:
                st.next_resend = sim.time
            if st.decision is None:
                st.next_prepare = sim.time + self.resend_interval
        self._arm_timer()

    # -- API -----------------------------------------------------------
    def begin(self, sim, txn, parts):
        if txn in self.txns:
            raise ValueError("duplicate txn %r" % txn)
        self.store.append({"op": "begin", "txn": txn, "parts": list(parts)})
        st = _Txn(parts)
        st.deadline = sim.time + self.vote_timeout
        st.next_prepare = sim.time + self.resend_interval
        self.txns[txn] = st
        for p in parts:
            sim.send(self.name, p, {"type": "PREPARE", "txn": txn})
        self._arm_timer()

    # -- message handling ----------------------------------------------
    def on_message(self, msg, sim):
        t = msg["type"]
        txn = msg["txn"]
        if t == "VOTE":
            st = self.txns.get(txn)
            if st is None or st.done:
                return
            if st.decision is not None:
                # participant apparently missed the decision; resend
                sim.send(self.name, msg["src"],
                         {"type": "DECISION", "txn": txn, "decision": st.decision})
                return
            if msg["src"] not in st.votes:
                st.votes[msg["src"]] = msg["vote"]
                self.store.append({"op": "vote", "txn": txn,
                                   "from": msg["src"], "vote": msg["vote"]})
            if msg["vote"] == "no":
                self._decide(sim, txn, "abort")
            elif len(st.votes) == len(st.parts) and \
                    all(v == "yes" for v in st.votes.values()):
                self._decide(sim, txn, "commit")
            self._arm_timer()
        elif t == "ACK":
            st = self.txns.get(txn)
            if st is not None and not st.done and msg["src"] not in st.acks:
                st.acks.add(msg["src"])
                self.store.append({"op": "ack", "txn": txn, "from": msg["src"]})
                if st.acks >= set(st.parts):
                    st.done = True
                    self.store.append({"op": "done", "txn": txn})
            self._arm_timer()
        elif t == "QUERY":
            st = self.txns.get(txn)
            decision = st.decision if st is not None else None
            sim.send(self.name, msg["src"],
                     {"type": "STATUS", "txn": txn, "decision": decision})

    # -- timers ----------------------------------------------------------
    def on_timer(self, sim):
        for txn, st in self.txns.items():
            if st.done:
                continue
            if st.decision is None:
                if st.next_prepare is not None and sim.time >= st.next_prepare:
                    for p in st.parts:
                        if p not in st.votes:
                            sim.send(self.name, p, {"type": "PREPARE", "txn": txn})
                    st.next_prepare = sim.time + self.resend_interval
                if st.deadline is not None and sim.time >= st.deadline:
                    self._decide(sim, txn, "abort")
            if st.decision is not None and \
                    st.next_resend is not None and sim.time >= st.next_resend:
                for p in st.parts:
                    if p not in st.acks:
                        sim.send(self.name, p,
                                 {"type": "DECISION", "txn": txn,
                                  "decision": st.decision})
                st.next_resend = sim.time + self.resend_interval
        self._arm_timer()

    # -- internals -------------------------------------------------------
    def _decide(self, sim, txn, decision):
        st = self.txns[txn]
        if st.decision is not None:
            return
        # persist the decision BEFORE sending it to anyone
        self.store.append({"op": "decide", "txn": txn, "decision": decision})
        st.decision = decision
        st.next_resend = sim.time

    def _arm_timer(self):
        times = []
        for st in self.txns.values():
            if st.done:
                continue
            if st.decision is None and st.deadline is not None:
                times.append(st.deadline)
            if st.decision is None and st.next_prepare is not None:
                times.append(st.next_prepare)
            if st.decision is not None and st.next_resend is not None:
                times.append(st.next_resend)
        self.next_timer_at = min(times) if times else None
