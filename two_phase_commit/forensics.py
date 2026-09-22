"""Read-only forensics for a damaged 2PC deployment.

The inspector reads coordinator and participant durable logs **without
modifying anything** and reports the evidence a human needs to decide how
to recover.  It never writes a decision, never truncates a log and never
starts the protocol: an automatic guess is exactly what must be avoided,
because a single participant APPLY(COMMIT) record makes COMMIT the only
legal global outcome.

Decision rules for one transaction
----------------------------------
1. Any participant (including one whose log is only readable as a prefix
   because its tail is torn) contains APPLY(COMMIT) -> COMMIT mandatory.
2. Any node contains a durable coordinator DECISION(COMMIT) -> COMMIT
   mandatory.
3. Any participant contains APPLY(ABORT) while rule 1/2 does not fire ->
   ABORT mandatory (a participant that voted NO or applied ABORT cannot be
   told to commit).
4. Durable DECISION(ABORT) and no commit evidence -> ABORT safe.
5. Participants are only PREPARED (or nothing) and no decision is
   readable:
   * coordinator log intact                    -> restart it; presumed
     abort applies; no human write needed.
   * coordinator log torn at a DECISION frame  -> AMBIGUOUS: the COMMIT
     may have been durable and lost to the tear; require manual
     inspection of every participant, then force-commit if *all* can
     commit, otherwise leave blocked and repair infrastructure.
6. Required participant logs cannot be read / are missing -> INCOMPLETE
   evidence; the decision is unsafe.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from . import storage


@dataclass
class NodeView:
    node_id: str
    role: str            # "coordinator" | "participant" | "unknown"
    path: str
    exists: bool
    records: list = field(default_factory=list)
    corruption: object = None

    def has(self, rtype, **match):
        for _seq, t, payload, _off in self.records:
            if t == rtype and all(payload.get(k) == v for k, v in match.items()):
                return True
        return False

    def payloads(self, rtype, **match):
        out = []
        for _seq, t, payload, _off in self.records:
            if t == rtype and all(payload.get(k) == v for k, v in match.items()):
                out.append(payload)
        return out


@dataclass
class TxnEvidence:
    txn: str
    coordinator: NodeView
    participants: list
    recommendation: str
    safe: bool
    findings: list = field(default_factory=list)
    actions: list = field(default_factory=list)
    blocked_participants: list = field(default_factory=list)
    committed_participants: list = field(default_factory=list)
    aborted_participants: list = field(default_factory=list)

    def as_dict(self):
        return {
            "txn": self.txn,
            "recommendation": self.recommendation,
            "safe": self.safe,
            "findings": self.findings,
            "actions": self.actions,
            "blocked_participants": self.blocked_participants,
            "committed_participants": self.committed_participants,
            "aborted_participants": self.aborted_participants,
            "coordinator": _view_dict(self.coordinator),
            "participants": [_view_dict(v) for v in self.participants],
        }


def _view_dict(view):
    return {
        "node_id": view.node_id,
        "role": view.role,
        "path": view.path,
        "exists": view.exists,
        "intact_records": len(view.records),
        "corruption": (
            None
            if view.corruption is None
            else {
                "error": str(view.corruption),
                "offset": getattr(view.corruption, "offset", None),
            }
        ),
    }


def inspect_node(node_id, path, role):
    if not os.path.exists(path):
        return NodeView(node_id, role, path, exists=False)
    records, corruption = storage.read_log_prefix(path)
    return NodeView(node_id, role, path, True, records, corruption)


def _infer_role(path):
    base = os.path.basename(path)
    if "coordinator" in base:
        return "coordinator"
    if base.startswith("participant") or "participant" in base:
        return "participant"
    return "unknown"


def scan_layout(paths):
    """Accept a coordinator file + participant files, or a data directory.

    Returns ``(coordinator_views, participant_views, txns)``.  In a
    directory the convention is ``coordinator.log`` and
    ``participant-<id>.log``; ``*.log`` files with inferred roles are also
    picked up.
    """

    coordinator_views = []
    participant_views = []
    expanded = []
    for p in paths:
        if os.path.isdir(p):
            for name in sorted(os.listdir(p)):
                if name.endswith(".log"):
                    expanded.append(os.path.join(p, name))
        else:
            expanded.append(p)
    if not paths:
        raise ValueError("no log paths supplied")

    for path in expanded:
        node_id = os.path.basename(path)[:-4] if path.endswith(".log") else path
        role = _infer_role(path)
        view = inspect_node(node_id, path, role)
        if role == "coordinator":
            coordinator_views.append(view)
        elif role == "participant":
            participant_views.append(view)
    # Fallback: no roles inferred -> treat the first view as coordinator.
    if not coordinator_views and participant_views:
        first = participant_views.pop(0)
        first.role = "coordinator"
        coordinator_views.append(first)

    txns = set()
    for view in coordinator_views + participant_views:
        for _seq, _t, payload, _off in view.records:
            if "txn" in payload:
                txns.add(payload["txn"])
    return coordinator_views, participant_views, sorted(txns)


def analyze_txn(txn, coordinator, participants):
    findings = []
    actions = []
    blocked, committed, aborted = [], [], []

    coord_decision = None
    coord_corrupt = coordinator is not None and coordinator.corruption is not None
    coord_exists = coordinator is not None and coordinator.exists
    if coordinator is not None:
        decs = coordinator.payloads("DECISION", txn=txn)
        if decs:
            coord_decision = decs[-1]["decision"]
            findings.append(
                "coordinator durable DECISION=%s (fsync'd; authoritative if log "
                "intact)" % coord_decision
            )
        if coordinator.has("END", txn=txn):
            findings.append("coordinator durable END present for %s" % txn)
        if coord_corrupt:
            findings.append(
                "coordinator log unreadable from offset %s: %s"
                % (coordinator.corruption.offset, coordinator.corruption)
            )
        if not coordinator.exists:
            findings.append("coordinator log missing")

    missing = []
    for view in participants:
        if not view.exists:
            missing.append(view.node_id)
            continue
        if view.corruption is not None:
            findings.append(
                "participant %s log torn at offset %s; prefix is all the "
                "evidence available" % (view.node_id, view.corruption.offset)
            )
        last_state = None
        if view.has("APPLY", txn=txn, outcome="COMMIT"):
            committed.append(view.node_id)
            last_state = "COMMITTED"
        elif view.has("APPLY", txn=txn, outcome="ABORT"):
            aborted.append(view.node_id)
            last_state = "ABORTED"
        elif view.has("VOTE", txn=txn, vote="VOTE_COMMIT"):
            blocked.append(view.node_id)
            last_state = "PREPARED"
        if view.corruption is not None and last_state != "COMMITTED":
            # A torn tail could hide an APPLY(COMMIT).
            findings.append(
                "participant %s tail is torn, a hidden COMMIT cannot be ruled "
                "out" % view.node_id
            )

    # Rule 1/2: commit evidence is absolute.
    if committed or coord_decision == "COMMIT":
        who = sorted(set(committed) | ({"coordinator"} if coord_decision == "COMMIT" else set()))
        findings.append("durable COMMIT evidence from: %s" % ", ".join(who))
        actions = [
            "Back up every log before touching anything.",
            "If the coordinator log is torn, rebuild a DECISION(COMMIT) record "
            "only via the documented manual force-commit procedure after "
            "confirming ALL participants can commit; a participant that already "
            "applied COMMIT proves abort is impossible.",
            "Restart the coordinator so committed participants that lost the "
            "message finish; keep blocked participants polling STATUS_QUERY.",
        ]
        return TxnEvidence(
            txn, coordinator, participants, "FORCE COMMIT", True,
            findings, actions, blocked, committed, aborted,
        )

    # Rule 3: any applied abort -> abort mandatory.
    if aborted:
        findings.append("participants with durable APPLY(ABORT): %s" % ", ".join(aborted))
        actions = [
            "Back up every log before touching anything.",
            "Re-establish a coordinator with durable DECISION(ABORT) via the "
            "manual force-abort procedure; COMMIT is impossible because at "
            "least one participant already aborted.",
        ]
        return TxnEvidence(
            txn, coordinator, participants, "FORCE ABORT", True,
            findings, actions, blocked, committed, aborted,
        )

    # Rule 4: durable abort decision with no conflicting evidence.
    if coord_decision == "ABORT":
        findings.append("durable DECISION(ABORT), no commit evidence anywhere")
        actions = [
            "Back up logs, then restart the coordinator; it will resend ABORT "
            "idempotently.",
        ]
        return TxnEvidence(
            txn, coordinator, participants, "ABORT (restart coordinator)", True,
            findings, actions, blocked, committed, aborted,
        )

    # Rule 5: no decision readable.
    if missing:
        findings.append("participant logs unreadable/missing: %s" % ", ".join(missing))
        actions = [
            "Do NOT decide anything until every missing log is recovered or "
            "every missing participant's resource owner is contacted; a hidden "
            "APPLY(COMMIT) would make abort a divergence.",
        ]
        return TxnEvidence(
            txn, coordinator, participants, "INSUFFICIENT EVIDENCE", False,
            findings, actions, blocked, committed, aborted,
        )

    if coord_corrupt:
        findings.append(
            "coordinator log is torn at a point that may have held the DECISION "
            "record; participants are merely PREPARED, so the outcome cannot be "
            "derived from durable evidence"
        )
        actions = [
            "Contact EVERY participant (and resource owner) listed as blocked; "
            "only if all confirm they can commit may a human force COMMIT.",
            "If any participant cannot commit, keep the transaction blocked and "
            "restore/rebuild the coordinator from replicas; never truncate the "
            "torn coordinator log silently.",
        ]
        return TxnEvidence(
            txn, coordinator, participants, "AMBIGUOUS - MANUAL DECISION REQUIRED",
            False, findings, actions, blocked, committed, aborted,
        )

    # Intact coordinator, no decision: presumed abort, automatic on restart.
    if coord_exists:
        findings.append(
            "coordinator log intact with no DECISION record; presumed abort: no "
            "COMMIT could ever have been sent (decisions are fsync'd before "
            "sends)"
        )
        actions = [
            "Simply restart the coordinator; recovery persists ABORT and drives "
            "phase 2. Participants currently PREPARED resolve via ABORT.",
        ]
        return TxnEvidence(
            txn, coordinator, participants, "ABORT (presumed - restart coordinator)",
            True, findings, actions, blocked, committed, aborted,
        )

    findings.append("no coordinator log and no readable decision")
    return TxnEvidence(
        txn, coordinator, participants, "INSUFFICIENT EVIDENCE", False,
        findings, ["Restore a coordinator log or contact every participant."],
        blocked, committed, aborted,
    )


def inspect(paths, txns=None):
    coordinators, participants, found_txns = scan_layout(paths)
    coordinator = coordinators[0] if coordinators else None
    targets = sorted(txns) if txns else found_txns
    return [analyze_txn(t, coordinator, participants) for t in targets]
