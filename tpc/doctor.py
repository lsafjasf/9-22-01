"""Read-only recovery advisor.

Scans every *.log in a data directory (coordinator log may be damaged),
collects evidence per transaction, and prints a suggested resolution.
It NEVER writes anything and NEVER guesses beyond what the evidence
supports; conflicting or insufficient evidence is reported as such.
"""
import os

from .logstore import LogStore


def analyze(dir):
    nodes = {}
    corrupt = {}
    for fname in sorted(os.listdir(dir)):
        if not fname.endswith(".log"):
            continue
        node = fname[:-4]
        records, bad = LogStore(os.path.join(dir, fname)).read_all()
        nodes[node] = records
        if bad:
            corrupt[node] = bad

    txns = {}

    def txn(t):
        return txns.setdefault(t, {
            "coordinator_decision": None,
            "participant_decisions": {},
            "votes": {},
            "prepared": [],
            "done": False,
        })

    coordinators = set()
    for node, records in nodes.items():
        for r in records:
            op = r.get("op")
            if op == "begin":
                coordinators.add(node)
                txn(r["txn"])
            elif op == "decide":
                coordinators.add(node)
                txn(r["txn"])["coordinator_decision"] = r["decision"]
            elif op == "done":
                txn(r["txn"])["done"] = True
            elif op == "prepared":
                e = txn(r["txn"])
                e["votes"][node] = r["vote"]
                if node not in e["prepared"]:
                    e["prepared"].append(node)
            elif op == "decision":
                txn(r["txn"])["participant_decisions"][node] = r["decision"]
            elif op == "conflict":
                txn(r["txn"]).setdefault("conflicts", []).append(
                    {"node": node, "have": r["have"], "got": r["got"]})

    report = {"dir": dir, "coordinators": sorted(coordinators),
              "corrupt": corrupt, "transactions": {}}
    for t, e in sorted(txns.items()):
        evidence_commit = (
            e["coordinator_decision"] == "commit"
            or "commit" in e["participant_decisions"].values()
        )
        evidence_abort = (
            e["coordinator_decision"] == "abort"
            or "abort" in e["participant_decisions"].values()
        )
        if evidence_commit and evidence_abort:
            suggestion, confidence = None, "none"
            reason = ("CONFLICT: evidence of both commit and abort exists; "
                      "manual intervention required, do NOT apply automatically")
        elif evidence_commit:
            suggestion, confidence = "commit", "high"
            reason = ("a commit decision is durable (coordinator decide record "
                      "and/or participant decision records); all participants "
                      "must commit")
        elif evidence_abort:
            suggestion, confidence = "abort", "high"
            reason = ("an abort decision is durable and no commit evidence "
                      "exists; all participants must abort")
        elif e["prepared"]:
            suggestion, confidence = "abort", "medium"
            reason = ("no decision was ever made durable anywhere; by 2PC "
                      "presume-abort no participant can have committed, so "
                      "abort is safe")
        else:
            suggestion, confidence = "abort", "low"
            reason = "transaction never reached any participant; nothing to undo"
        if corrupt and confidence == "high":
            confidence = "medium"
            reason += " (caveat: some logs are corrupt, evidence may be incomplete)"
        entry = dict(e)
        entry.update({"suggestion": suggestion, "confidence": confidence,
                      "reason": reason})
        report["transactions"][t] = entry
    return report


def format_report(report):
    lines = []
    lines.append("== 2PC 只读恢复报告 (read-only, 未修改任何文件) ==")
    lines.append("目录: %s" % report["dir"])
    if report["corrupt"]:
        lines.append("!! 检测到损坏的日志:")
        for node, bad in report["corrupt"].items():
            for line_no, raw in bad:
                lines.append("   %s.log 第%d行: %r" % (node, line_no, raw[:80]))
    for t, e in report["transactions"].items():
        lines.append("")
        lines.append("事务 %s:" % t)
        lines.append("  协调者决议: %s" % (e["coordinator_decision"] or "无记录"))
        if e["votes"]:
            lines.append("  参与方投票: %s" %
                         ", ".join("%s=%s" % kv for kv in sorted(e["votes"].items())))
        if e["participant_decisions"]:
            lines.append("  参与方决议: %s" %
                         ", ".join("%s=%s" % kv
                                   for kv in sorted(e["participant_decisions"].items())))
        if e.get("conflicts"):
            lines.append("  !! 冲突记录: %s" % e["conflicts"])
        lines.append("  建议决议: %s (置信度: %s)" %
                     (e["suggestion"] or "无法建议", e["confidence"]))
        lines.append("  依据: %s" % e["reason"])
    if not report["transactions"]:
        lines.append("未发现任何事务记录。")
    return "\n".join(lines)
