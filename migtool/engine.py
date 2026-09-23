"""执行引擎：断点续跑日志、审计、分块执行、强杀重入。"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from .model import Migration, Step


class SimulatedKill(Exception):
    """测试用：模拟进程被强杀。"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def convert_value(value: Any, conv: dict) -> Any:
    """受限的声明式值转换器（不使用 eval）。"""
    if value is None:
        return None
    kind = conv.get("kind", "cast")
    if kind == "cast":
        to = conv["to"].upper()
        try:
            if to == "INTEGER":
                return int(float(value))
            if to == "REAL":
                return float(value)
            if to == "TEXT":
                return str(value)
            if to == "NUMERIC":
                f = float(value)
                return int(f) if f.is_integer() else f
        except (TypeError, ValueError):
            if conv.get("on_error") == "null":
                return None
            if "on_error_value" in conv:
                return conv["on_error_value"]
            raise
        return value
    raise ValueError(f"未知转换类型: {kind}")


def split_value(value: Any, delimiter: str, n: int) -> list[Any]:
    if value is None:
        return [None] * n
    parts = str(value).split(delimiter, n - 1)
    parts = [x.strip() for x in parts]
    while len(parts) < n:
        parts.append("")
    return parts


class Engine:
    """在单个 sqlite 库上执行迁移。所有批次各自提交，崩溃后可从日志游标恢复。"""

    def __init__(self, db_path: str, migration: Migration,
                 fault_hook: Callable[[], None] | None = None,
                 strategy: str = "apply"):
        self.db_path = db_path
        self.migration = migration
        self.fault_hook = fault_hook
        self.strategy = strategy
        self.stats: dict[tuple[str, str], int] = {}
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=OFF")
        self._ensure_meta()

    def close(self) -> None:
        self.conn.close()

    # ---------- 元表：journal + audit ----------
    def _ensure_meta(self) -> None:
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS _mig_journal (
            migration_id TEXT NOT NULL,
            step_id     TEXT NOT NULL,
            direction   TEXT NOT NULL,      -- forward | rollback
            phase       TEXT NOT NULL,      -- 步骤内的阶段名
            status      TEXT NOT NULL,      -- running | done
            cursor      INTEGER NOT NULL DEFAULT -1,
            updated_at  TEXT NOT NULL,
            PRIMARY KEY (migration_id, step_id, direction, phase)
        );
        CREATE TABLE IF NOT EXISTS _mig_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            event TEXT NOT NULL,
            migration_id TEXT,
            step_id TEXT,
            detail TEXT
        );
        CREATE TABLE IF NOT EXISTS _mig_backup_meta (
            migration_id TEXT NOT NULL,
            step_id TEXT NOT NULL,
            payload TEXT NOT NULL,
            PRIMARY KEY (migration_id, step_id)
        );
        """)
        self.conn.commit()

    def audit(self, event: str, step_id: str | None = None, detail: dict | None = None) -> None:
        self.conn.execute(
            "INSERT INTO _mig_audit(ts,event,migration_id,step_id,detail) VALUES(?,?,?,?,?)",
            (utcnow(), event, self.migration.migration_id, step_id,
             json.dumps(detail or {}, ensure_ascii=False)))
        self.conn.commit()

    # ---------- journal 状态 ----------
    def _jkey(self, step_id: str, direction: str, phase: str) -> tuple:
        return (self.migration.migration_id, step_id, direction, phase)

    def phase_status(self, step_id: str, direction: str, phase: str) -> tuple[str, int] | None:
        row = self.conn.execute(
            "SELECT status, cursor FROM _mig_journal WHERE migration_id=? AND step_id=? "
            "AND direction=? AND phase=?",
            self._jkey(step_id, direction, phase)).fetchone()
        return (row["status"], row["cursor"]) if row else None

    def _set_phase(self, step_id: str, direction: str, phase: str,
                   status: str, cursor: int) -> None:
        self.conn.execute(
            "INSERT INTO _mig_journal(migration_id,step_id,direction,phase,status,cursor,updated_at) "
            "VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(migration_id,step_id,direction,phase) "
            "DO UPDATE SET status=excluded.status, cursor=excluded.cursor, updated_at=excluded.updated_at",
            (*self._jkey(step_id, direction, phase), status, cursor, utcnow()))

    def step_done(self, step_id: str, direction: str = "forward") -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM _mig_journal WHERE migration_id=? AND step_id=? AND direction=? "
            "AND phase='__step__' AND status='done'",
            (self.migration.migration_id, step_id, direction)).fetchone()
        return row is not None

    def step_touched(self, step_id: str, direction: str = "forward") -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM _mig_journal WHERE migration_id=? AND step_id=? AND direction=?",
            (self.migration.migration_id, step_id, direction)).fetchone()
        return row is not None

    def mark_step_done(self, step_id: str, direction: str) -> None:
        self._set_phase(step_id, direction, "__step__", "done", -1)
        self.conn.commit()

    # ---------- 分块执行 ----------
    def run_chunked(self, step: Step, direction: str, phase: str,
                    fetch: Callable[[int, int], list[sqlite3.Row]],
                    apply_batch: Callable[[list[sqlite3.Row]], None]) -> int:
        """按批处理行。fetch(cursor, limit) 必须返回 rid 列（rowid 别名）、按 rid 升序且幂等。
        每批提交一次并推进游标；崩溃后从游标继续，已提交批次不会重放。"""
        st = self.phase_status(step.id, direction, phase)
        if st and st[0] == "done":
            return 0
        key = (step.id, phase)
        cursor = st[1] if st else -1
        self._set_phase(step.id, direction, phase, "running", cursor)
        self.conn.commit()
        processed = 0
        while True:
            rows = fetch(cursor, self.migration.batch_size)
            if not rows:
                break
            apply_batch(rows)
            cursor = int(rows[-1]["rid"])
            processed += len(rows)
            self.stats[key] = self.stats.get(key, 0) + len(rows)
            self._set_phase(step.id, direction, phase, "running", cursor)
            self.conn.commit()
            if self.fault_hook is not None:
                self.fault_hook()  # 测试注入：此处抛异常即模拟强杀（批次已提交）
        self._set_phase(step.id, direction, phase, "done", cursor)
        self.conn.commit()
        return processed

    # ---------- schema 辅助 ----------
    def columns(self) -> dict[str, str]:
        return {r["name"]: r["type"] for r in
                self.conn.execute(f"PRAGMA table_info({self.migration.table})")}

    def has_column(self, name: str) -> bool:
        return name in self.columns()

    def index_exists(self, name: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (name,)).fetchone()
        return row is not None

    def backup_table(self, step: Step) -> str:
        safe = "".join(c if c.isalnum() else "_" for c in step.id)
        return f"_migbak_{safe}"

    def save_backup_meta(self, step: Step, payload: dict) -> None:
        self.conn.execute(
            "INSERT INTO _mig_backup_meta(migration_id,step_id,payload) VALUES(?,?,?) "
            "ON CONFLICT(migration_id,step_id) DO UPDATE SET payload=excluded.payload",
            (self.migration.migration_id, step.id, json.dumps(payload, ensure_ascii=False)))
        self.conn.commit()

    def load_backup_meta(self, step: Step) -> dict | None:
        row = self.conn.execute(
            "SELECT payload FROM _mig_backup_meta WHERE migration_id=? AND step_id=?",
            (self.migration.migration_id, step.id)).fetchone()
        return json.loads(row["payload"]) if row else None

    # ---------- 顶层流程 ----------
    def apply(self) -> None:
        from .steps import forward_step
        self.audit("migration_start", detail={"strategy": self.strategy,
                                              "description": self.migration.description})
        for step in self.migration.ordered_steps():
            if self.step_done(step.id, "forward"):
                self.audit("step_skip_done", step.id)
                continue
            self.audit("step_start", step.id, {"type": step.type, "params": step.params})
            t0 = time.monotonic()
            forward_step(self, step)
            self.mark_step_done(step.id, "forward")
            self.audit("step_done", step.id,
                       {"elapsed_ms": round((time.monotonic() - t0) * 1000, 2)})
        self.audit("migration_done", detail={"strategy": self.strategy})

    def rollback(self) -> None:
        from .steps import rollback_step
        self.audit("rollback_start")
        for step in reversed(self.migration.ordered_steps()):
            if self.step_done(step.id, "rollback"):
                self.audit("step_skip_done", step.id, {"direction": "rollback"})
                continue
            if not self.step_touched(step.id, "forward"):
                self.audit("step_skip_never_applied", step.id)
                continue
            self.audit("rollback_step_start", step.id, {"type": step.type})
            t0 = time.monotonic()
            rollback_step(self, step)
            self.mark_step_done(step.id, "rollback")
            self.audit("rollback_step_done", step.id,
                       {"elapsed_ms": round((time.monotonic() - t0) * 1000, 2)})
        self.audit("rollback_done")

    def audit_rows(self) -> Iterable[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM _mig_audit WHERE migration_id=? ORDER BY id",
            (self.migration.migration_id,))
