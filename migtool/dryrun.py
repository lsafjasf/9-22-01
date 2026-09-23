"""副本预演：在数据库副本上完整执行迁移，统计变更量并预估耗时。"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import time
from datetime import datetime, timezone

from .compat import evaluate, format_report
from .engine import Engine
from .model import Migration


def make_replica(src_path: str, table: str, sample_rows: int | None = None) -> str:
    """用 sqlite backup API 复制数据库；sample_rows 时只复制前 N 行数据用于抽样预演。"""
    fd, dst = tempfile.mkstemp(prefix="migtool_replica_", suffix=".db")
    os.close(fd)
    src = sqlite3.connect(src_path)
    if sample_rows is None:
        dst_conn = sqlite3.connect(dst)
        src.backup(dst_conn)
        dst_conn.close()
    else:
        dst_conn = sqlite3.connect(dst)
        cols = [r[1] for r in src.execute(f"PRAGMA table_info({table})")]
        col_list = ", ".join(f'"{c}"' for c in cols)
        schema = src.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
        dst_conn.execute(schema)
        rows = src.execute(
            f"SELECT {col_list} FROM {table} ORDER BY rowid LIMIT ?", (sample_rows,)).fetchall()
        dst_conn.executemany(
            f"INSERT INTO {table} ({col_list}) VALUES ({','.join('?' * len(cols))})", rows)
        dst_conn.commit()
        dst_conn.close()
    src.close()
    return dst


def _table_fingerprint(db_path: str, table: str) -> str:
    conn = sqlite3.connect(db_path)
    cols = sorted(r[1] for r in conn.execute(f"PRAGMA table_info({table})"))
    col_list = ", ".join(f'"{c}"' for c in cols)
    h = hashlib.sha256()
    h.update("|".join(cols).encode())
    for row in conn.execute(f"SELECT {col_list} FROM {table} ORDER BY rowid"):
        h.update(repr(tuple(row)).encode())
    conn.close()
    return h.hexdigest()


def dry_run(db_path: str, migration: Migration, sample_rows: int | None = None,
            verify_rollback: bool = True) -> dict:
    prod_rows = sqlite3.connect(db_path).execute(
        f"SELECT COUNT(*) FROM {migration.table}").fetchone()[0]
    replica = make_replica(db_path, migration.table, sample_rows)
    replica_rows = prod_rows if sample_rows is None else min(sample_rows, prod_rows)
    try:
        eng = Engine(replica, migration)
        before_fp = _table_fingerprint(replica, migration.table)
        step_stats = []
        t_all = time.monotonic()
        for step in migration.ordered_steps():
            from .steps import forward_step
            t0 = time.monotonic()
            forward_step(eng, step)
            eng.mark_step_done(step.id, "forward")
            elapsed = time.monotonic() - t0
            affected = sum(v for (sid, _), v in eng.stats.items() if sid == step.id)
            scale = prod_rows / replica_rows if replica_rows else 1.0
            step_stats.append({
                "step_id": step.id,
                "type": step.type,
                "affected_rows": affected,
                "measured_ms": round(elapsed * 1000, 2),
                "estimated_full_ms": round(elapsed * 1000 * scale, 2),
            })
        total_ms = (time.monotonic() - t_all) * 1000
        scale = prod_rows / replica_rows if replica_rows else 1.0

        fidelity = "not_verified"
        if verify_rollback:
            eng.rollback()
            after_fp = _table_fingerprint(replica, migration.table)
            fidelity = "exact" if after_fp == before_fp else "DIFFERS"
        eng.close()

        return {
            "migration_id": migration.migration_id,
            "table": migration.table,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "replica_rows": replica_rows,
            "production_rows": prod_rows,
            "steps": step_stats,
            "total_measured_ms": round(total_ms, 2),
            "total_estimated_ms": round(total_ms * scale, 2),
            "rollback_fidelity": fidelity,
            "compat_report": format_report(evaluate(migration)),
        }
    finally:
        os.unlink(replica)


def write_report(report: dict, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
