"""迁移执行引擎。

关键设计：

- **可中断 / 可重入**：每个步骤的 apply_record 幂等；每条记录在变换前
  先把原始字段追加写入快照文件（fsync），再写记录本身。崩溃后重启时，
  快照中已有的记录直接幂等重放（无副作用），未处理的记录继续处理。
  批次提交后落盘检查点（state.json）。
- **可回滚**：回滚 = 逆序对每个步骤「删除产出字段 + 写回快照原值」，
  因此回滚后数据语义与迁移前完全一致（对步骤触碰的字段而言）。
- **审计**：所有事件追加写入 .migration/audit.jsonl。
"""

import json
import os
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from .spec import Step
from .steps import BREAKING, SAFE, WARNING, get_handler
from .store import Store, _atomic_write_json


class NeedsConfirmation(Exception):
    """存在破坏性步骤且未显式确认。"""

    def __init__(self, report):
        super().__init__("存在破坏性步骤，需显式确认后方可执行")
        self.report = report


class RollbackError(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class _Snapshot:
    """单个步骤的快照文件（append-only JSONL）：{"id": ..., "fields": {...}}"""

    def __init__(self, path: Path):
        self.path = path
        self.entries = {}
        if path.exists():
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        entry = json.loads(line)
                        self.entries[entry["id"]] = entry["fields"]
        self._fh = None

    def __contains__(self, rid):
        return rid in self.entries

    def append(self, rid, fields):
        if self._fh is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(self.path, "a", encoding="utf-8")
        self._fh.write(json.dumps({"id": rid, "fields": fields},
                                  ensure_ascii=False) + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self.entries[rid] = fields

    def close(self):
        if self._fh is not None:
            self._fh.close()
            self._fh = None


class Engine:
    def __init__(self, store: Store, spec=None, batch_size: int = 100,
                 on_batch=None):
        """
        on_batch: 可选回调 fn(step_id, batch_no)，每批提交后调用，
                  测试用它注入「强杀」。
        """
        self.store = store
        self.spec = spec
        self.batch_size = batch_size
        self.on_batch = on_batch
        self.state_path = store.meta_dir / "state.json"
        self.audit_path = store.meta_dir / "audit.jsonl"
        self.snap_dir = store.meta_dir / "snapshots"

    # ---------- 状态与审计 ----------
    def _load_state(self) -> dict:
        if self.state_path.exists():
            with open(self.state_path, encoding="utf-8") as fh:
                return json.load(fh)
        return {"steps": {}, "applied": []}

    def _save_state(self, state: dict) -> None:
        _atomic_write_json(self.state_path, state)

    def _audit(self, event: str, **detail) -> None:
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": _now(), "event": event, **detail}
        with open(self.audit_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    # ---------- 兼容性评估 ----------
    def evaluate(self) -> dict:
        report = {"migration": self.spec.name,
                  "from_version": self.spec.from_version,
                  "to_version": self.spec.to_version,
                  "steps": []}
        rank = {SAFE: 0, WARNING: 1, BREAKING: 2}
        overall = SAFE
        for step in self.spec.steps:
            handler = get_handler(step.type)
            issues = [{"level": lv, "reason": rs}
                      for lv, rs in handler.compat(step)]
            level = max((i["level"] for i in issues), key=rank.get,
                        default=SAFE)
            overall = max(overall, level, key=rank.get)
            report["steps"].append({
                "id": step.id, "type": step.type, "level": level,
                "depends_on": step.depends_on, "issues": issues,
            })
        report["overall"] = overall
        report["requires_confirmation"] = overall == BREAKING
        return report

    # ---------- 前向执行 ----------
    def run(self, allow_breaking: bool = False, purpose: str = "run") -> dict:
        report = self.evaluate()
        if report["requires_confirmation"] and not allow_breaking:
            self._audit("confirmation_refused", migration=self.spec.name)
            raise NeedsConfirmation(report)

        state = self._load_state()
        state["migration"] = self.spec.name
        state["to_version"] = self.spec.to_version
        self._audit("migration_started", migration=self.spec.name,
                    purpose=purpose,
                    breaking_confirmed=report["requires_confirmation"])
        results = []
        for step in self.spec.steps:
            if state["steps"].get(step.id, {}).get("status") == "done":
                self._audit("step_skipped_done", step=step.id)
                continue
            results.append(self._run_step(step, state))
        self._save_state(state)
        self._audit("migration_done", migration=self.spec.name)
        return {"evaluate": report, "results": results}

    def repair(self) -> dict:
        """前向修复：不重做兼容性确认，幂等地把未完成的步骤推进到完成。"""
        state = self._load_state()
        if not state.get("migration"):
            raise RollbackError("没有进行中的迁移，无需修复")
        self._audit("repair_started", migration=self.spec.name)
        return self.run(allow_breaking=True, purpose="repair")

    def _run_step(self, step: Step, state: dict) -> dict:
        handler = get_handler(step.type)
        self._audit("step_started", step=step.id, type=step.type)
        state["steps"][step.id] = {"status": "in_progress", "processed": 0}
        self._save_state(state)
        started = time.monotonic()

        if handler.record_level:
            examined, changed = self._run_record_step(step, handler, state)
            extra = {}
        else:
            meta_path = self.snap_dir / f"{step.id}.meta.json"
            if not meta_path.exists():
                _atomic_write_json(meta_path, {
                    "index_existed": self.store.index_exists(step.get("index"))})
            keys = handler.apply_store(self.store, step)
            examined = changed = self.store.count()
            extra = {"index_keys": keys}

        state["steps"][step.id] = {"status": "done", "processed": examined}
        if step.id not in state["applied"]:
            state["applied"].append(step.id)
        self._save_state(state)
        elapsed = time.monotonic() - started
        self._audit("step_done", step=step.id, examined=examined,
                    changed=changed, elapsed=round(elapsed, 6), **extra)
        return {"id": step.id, "type": step.type, "examined": examined,
                "changed": changed, "elapsed": elapsed, **extra}

    def _run_record_step(self, step: Step, handler, state: dict):
        snap = _Snapshot(self.snap_dir / f"{step.id}.jsonl")
        examined = changed = 0
        snap_fields = handler.snapshot_fields(step)
        try:
            ids = self.store.list_ids()
            for batch_no in range(0, len(ids), self.batch_size):
                batch = ids[batch_no:batch_no + self.batch_size]
                for rid in batch:
                    doc = self.store.read(rid)
                    if rid not in snap:
                        # 先落快照（fsync），再改记录：
                        # 崩溃后重启可从快照幂等重放，不会重复副作用。
                        snap.append(rid, {f: doc[f] for f in snap_fields
                                          if f in doc})
                    if handler.apply_record(doc, step):
                        self.store.write(rid, doc)
                        changed += 1
                    examined += 1
                state["steps"][step.id]["processed"] = examined
                self._save_state(state)
                self._audit("batch_committed", step=step.id,
                            processed=examined)
                if self.on_batch:
                    self.on_batch(step.id, batch_no // self.batch_size)
        finally:
            snap.close()
        return examined, changed

    # ---------- 回滚 ----------
    def rollback(self, to_step: str = None, all_steps: bool = False) -> dict:
        state = self._load_state()
        applied = state.get("applied", [])
        if not applied:
            raise RollbackError("没有已应用的步骤，无法回滚")
        if not all_steps and to_step is None:
            # 默认只回滚最后一步
            targets = applied[-1:]
        elif all_steps:
            targets = list(applied)
        else:
            if to_step not in applied:
                raise RollbackError(f"步骤 {to_step!r} 未应用，无法回滚到它")
            idx = applied.index(to_step)
            targets = applied[idx + 1:]

        by_id = {s.id: s for s in self.spec.steps} if self.spec else {}
        rolled = []
        self._audit("rollback_started", targets=list(reversed(targets)))
        for sid in reversed(targets):
            step = by_id.get(sid)
            if step is None:
                raise RollbackError(f"当前迁移描述中找不到已应用的步骤 {sid!r}，"
                                    f"请使用执行迁移时的同一份描述文件")
            self._rollback_step(step)
            state["applied"].remove(sid)
            state["steps"][sid] = {"status": "rolled_back"}
            self._save_state(state)
            rolled.append(sid)
        self._audit("rollback_done", rolled_back=rolled)
        return {"rolled_back": rolled}

    def _rollback_step(self, step: Step) -> None:
        handler = get_handler(step.type)
        self._audit("step_rollback_started", step=step.id)
        if not handler.record_level:
            meta_path = self.snap_dir / f"{step.id}.meta.json"
            meta = {"index_existed": False}
            if meta_path.exists():
                with open(meta_path, encoding="utf-8") as fh:
                    meta = json.load(fh)
            name = step.get("index")
            if meta.get("index_existed"):
                self.store.rebuild_index(name, step.get("field"))
            else:
                self.store.drop_index(name)
            self._audit("step_rolled_back", step=step.id)
            return

        snap = _Snapshot(self.snap_dir / f"{step.id}.jsonl")
        outputs = handler.output_fields(step)
        snap_fields = handler.snapshot_fields(step)
        restored = 0
        for rid, fields in snap.entries.items():
            try:
                doc = self.store.read(rid)
            except FileNotFoundError:
                self._audit("rollback_record_missing", step=step.id, record=rid)
                continue
            for f in outputs + snap_fields:
                doc.pop(f, None)
            doc.update(fields)
            self.store.write(rid, doc)
            restored += 1
        snap.close()
        self._audit("step_rolled_back", step=step.id, restored=restored)

    # ---------- 状态查询 ----------
    def status(self) -> dict:
        state = self._load_state()
        return {"migration": state.get("migration"),
                "to_version": state.get("to_version"),
                "applied": state.get("applied", []),
                "steps": state.get("steps", {}),
                "records": self.store.count()}


# ---------- 副本预演 ----------
def dry_run(store: Store, spec, sample: int = None, batch_size: int = 100) -> dict:
    """在副本上完整执行迁移，统计变更量并预估耗时。不改动原库。"""
    tmp = Path(tempfile.mkdtemp(prefix="migtool-dryrun-"))
    started = time.monotonic()
    try:
        replica_root = tmp / "replica"
        replica_root.mkdir()
        (replica_root / "records").mkdir()
        ids = store.list_ids()
        if sample is not None and sample < len(ids):
            ids = ids[:sample]
        for rid in ids:
            shutil.copy2(store._record_path(rid),
                         replica_root / "records" / f"{rid}.json")
        replica = Store(replica_root)
        engine = Engine(replica, spec, batch_size=batch_size)
        outcome = engine.run(allow_breaking=True, purpose="dry-run")
        elapsed = time.monotonic() - started

        total_records = store.count()
        replayed = len(ids)
        scale = total_records / replayed if replayed else 1.0
        return {
            "dry_run": True,
            "migration": spec.name,
            "records_total": total_records,
            "records_rehearsed": replayed,
            "compatibility": outcome["evaluate"],
            "steps": [
                {**r, "elapsed": round(r["elapsed"], 6),
                 "changed_estimated": round(r["changed"] * scale)}
                for r in outcome["results"]
            ],
            "changed_total": sum(r["changed"] for r in outcome["results"]),
            "changed_total_estimated": round(
                sum(r["changed"] for r in outcome["results"]) * scale),
            "elapsed_seconds": round(elapsed, 6),
            "estimated_seconds_full": round(elapsed * scale, 3),
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
