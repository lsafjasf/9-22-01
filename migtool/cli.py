"""命令行入口：check / dry-run / apply / rollback / status / audit / init-demo。"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys

from .compat import evaluate, format_report, has_breaking
from .dryrun import dry_run, write_report
from .engine import Engine
from .model import MigrationError, load_migration


def _load(args):
    try:
        return load_migration(args.migration)
    except (MigrationError, KeyError, json.JSONDecodeError) as e:
        print(f"迁移描述无效: {e}", file=sys.stderr)
        sys.exit(2)


def cmd_check(args) -> int:
    mig = _load(args)
    findings = evaluate(mig)
    print(f"兼容性评估: {mig.migration_id} (表 {mig.table})")
    print(format_report(findings))
    if has_breaking(findings):
        print("\n存在破坏性步骤：未经显式确认（--confirm-breaking）不得执行。")
        return 1
    print("\n未发现破坏性步骤。")
    return 0


def cmd_dry_run(args) -> int:
    mig = _load(args)
    report = dry_run(args.db, mig, sample_rows=args.sample_rows,
                     verify_rollback=not args.no_verify_rollback)
    out = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        write_report(report, args.report)
        print(f"预演报告已写入 {args.report}")
    print(out)
    return 0


def cmd_apply(args) -> int:
    mig = _load(args)
    findings = evaluate(mig)
    print(format_report(findings))
    if has_breaking(findings) and not args.confirm_breaking:
        print("\n拒绝执行：存在破坏性步骤。确认旧版本已兼容后，加 --confirm-breaking 重试。",
              file=sys.stderr)
        return 1
    eng = Engine(args.db, mig, strategy=args.strategy)
    try:
        eng.apply()
    finally:
        eng.close()
    print("迁移完成。")
    return 0


def cmd_rollback(args) -> int:
    mig = _load(args)
    eng = Engine(args.db, mig, strategy="rollback")
    try:
        eng.rollback()
    finally:
        eng.close()
    print("回滚完成。")
    return 0


def _has_meta(conn, table):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,)).fetchone() is not None


def cmd_status(args) -> int:
    mig = _load(args)
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    if not _has_meta(conn, "_mig_journal"):
        print("（无执行记录）")
        return 0
    rows = conn.execute(
        "SELECT step_id, direction, phase, status, cursor, updated_at FROM _mig_journal "
        "WHERE migration_id=? ORDER BY step_id, direction, phase",
        (mig.migration_id,)).fetchall()
    if not rows:
        print("（无执行记录）")
        return 0
    for r in rows:
        print(f"  {r['step_id']:<24} {r['direction']:<8} {r['phase']:<18} "
              f"{r['status']:<8} cursor={r['cursor']:<8} {r['updated_at']}")
    return 0


def cmd_audit(args) -> int:
    mig = _load(args)
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    if not _has_meta(conn, "_mig_audit"):
        print("（无审计记录）")
        return 0
    rows = conn.execute(
        "SELECT * FROM _mig_audit WHERE migration_id=? ORDER BY id",
        (mig.migration_id,)).fetchall()
    for r in rows:
        step = r["step_id"] or "-"
        print(f"  {r['ts']}  {r['event']:<26} {step:<24} {r['detail'] or ''}")
    return 0


def cmd_init_demo(args) -> int:
    conn = sqlite3.connect(args.db)
    conn.execute("DROP TABLE IF EXISTS users")
    conn.execute("""CREATE TABLE users (
        id INTEGER PRIMARY KEY,
        full_name TEXT,
        age TEXT,
        city TEXT,
        street TEXT,
        status TEXT,
        legacy_flag TEXT
    )""")
    names = ["Zhang San", "Li Si", "Wang Wu", "Zhao Liu", "Chen Qi"]
    cities = ["Beijing", "Shanghai", "Shenzhen", "Hangzhou", "Chengdu"]
    rows = []
    for i in range(args.rows):
        n = names[i % len(names)]
        rows.append((f"{n} {i}", str(20 + i % 50), cities[i % len(cities)],
                     f"No.{i} Main Rd", None if i % 3 else "active",
                     f"flag{i % 7}"))
    conn.executemany(
        "INSERT INTO users(full_name,age,city,street,status,legacy_flag) "
        "VALUES(?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    print(f"演示库已创建: {args.db}（users 表 {args.rows} 行）")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="migtool", description="声明式数据迁移工具")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, need_db=True):
        if need_db:
            p.add_argument("--db", required=True, help="sqlite 数据库路径")
        p.add_argument("--migration", required=True, help="迁移描述 JSON 路径")

    p = sub.add_parser("check", help="兼容性评估（不执行）")
    common(p)
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("dry-run", help="在副本上预演并输出报告")
    common(p)
    p.add_argument("--sample-rows", type=int, default=None, help="抽样行数（默认全量副本）")
    p.add_argument("--report", help="报告输出路径（JSON）")
    p.add_argument("--no-verify-rollback", action="store_true", help="跳过回滚保真校验")
    p.set_defaults(fn=cmd_dry_run)

    p = sub.add_parser("apply", help="执行迁移（可断点续跑）")
    common(p)
    p.add_argument("--confirm-breaking", action="store_true",
                   help="显式确认执行破坏性步骤")
    p.add_argument("--strategy", choices=["apply", "forward-fix"], default="apply",
                   help="apply=正常迁移；forward-fix=前向修复（补偿式迁移，审计中标记）")
    p.set_defaults(fn=cmd_apply)

    p = sub.add_parser("rollback", help="回滚已执行的迁移")
    common(p)
    p.set_defaults(fn=cmd_rollback)

    p = sub.add_parser("status", help="查看迁移执行状态")
    common(p)
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("audit", help="查看审计记录")
    common(p)
    p.set_defaults(fn=cmd_audit)

    p = sub.add_parser("init-demo", help="生成演示数据库")
    p.add_argument("--db", required=True)
    p.add_argument("--rows", type=int, default=10000)
    p.set_defaults(fn=cmd_init_demo)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
