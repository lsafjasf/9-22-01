"""命令行入口：python -m migtool <command> ..."""

import argparse
import json
import random
import sys

from .engine import Engine, NeedsConfirmation, RollbackError, dry_run
from .spec import SpecError, load_spec
from .store import Store


def _print_json(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _print_evaluate(report) -> None:
    mark = {"safe": "[安全]", "warning": "[警告]", "breaking": "[破坏]"}
    print(f"迁移 {report['migration']}: "
          f"v{report['from_version']} -> v{report['to_version']}, "
          f"总体级别: {report['overall']}")
    for step in report["steps"]:
        print(f"  {mark[step['level']]} {step['id']} ({step['type']})")
        for issue in step["issues"]:
            print(f"      {mark[issue['level']]} {issue['reason']}")


def cmd_init_demo(args) -> int:
    store = Store(args.store)
    rng = random.Random(args.seed)
    first = ["Alice", "Bob", "Carol", "David", "Eve", "Frank", "Grace"]
    last = ["Smith", "Lee", "Wang", "Garcia", "Brown", "Miller"]
    for i in range(args.records):
        rid = f"u{i:05d}"
        store.write(rid, {
            "id": rid,
            "name": f"{rng.choice(first)} {rng.choice(last)}",
            "age": str(rng.randint(18, 70)),
            "email": f"user{i}@example.com",
            "tags": rng.sample(["vip", "trial", "internal", "beta"], k=2),
        })
    print(f"已在 {args.store} 生成 {args.records} 条演示记录")
    return 0


def cmd_check(args) -> int:
    engine = Engine(Store(args.store), load_spec(args.spec))
    report = engine.evaluate()
    _print_evaluate(report)
    if args.json:
        _print_json(report)
    return 2 if report["requires_confirmation"] else 0


def cmd_dry_run(args) -> int:
    report = dry_run(Store(args.store), load_spec(args.spec),
                     sample=args.sample, batch_size=args.batch_size)
    _print_evaluate(report["compatibility"])
    print(f"\n预演完成：演练 {report['records_rehearsed']}/"
          f"{report['records_total']} 条记录")
    for step in report["steps"]:
        print(f"  {step['id']}: 变更 {step['changed']} 条"
              f"（全量预估 {step['changed_estimated']} 条）, "
              f"耗时 {step['elapsed']:.4f}s")
    print(f"总变更量预估 {report['changed_total_estimated']} 条, "
          f"全量预估耗时 {report['estimated_seconds_full']}s")
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        print(f"报告已写入 {args.report}")
    return 0


def cmd_run(args) -> int:
    engine = Engine(Store(args.store), load_spec(args.spec),
                    batch_size=args.batch_size)
    try:
        outcome = engine.run(allow_breaking=args.yes)
    except NeedsConfirmation as exc:
        _print_evaluate(exc.report)
        print("\n存在破坏性步骤，已拒绝执行。"
              "确认旧版本已兼容后，加 --yes 重新执行。")
        return 2
    for r in outcome["results"]:
        print(f"  完成 {r['id']}: 扫描 {r['examined']}, 变更 {r['changed']}, "
              f"耗时 {r['elapsed']:.4f}s")
    print("迁移完成")
    return 0


def cmd_rollback(args) -> int:
    engine = Engine(Store(args.store), load_spec(args.spec))
    try:
        result = engine.rollback(to_step=args.to_step, all_steps=args.all)
    except RollbackError as exc:
        print(f"回滚失败: {exc}", file=sys.stderr)
        return 1
    print(f"已回滚步骤: {result['rolled_back']}")
    return 0


def cmd_repair(args) -> int:
    engine = Engine(Store(args.store), load_spec(args.spec),
                    batch_size=args.batch_size)
    try:
        outcome = engine.repair()
    except RollbackError as exc:
        print(f"修复失败: {exc}", file=sys.stderr)
        return 1
    done = [r["id"] for r in outcome["results"]]
    print(f"前向修复完成，推进步骤: {done or '（无待办步骤）'}")
    return 0


def cmd_status(args) -> int:
    _print_json(Engine(Store(args.store)).status())
    return 0


def cmd_audit(args) -> int:
    path = Store(args.store).meta_dir / "audit.jsonl"
    if not path.exists():
        print("（暂无审计记录）")
        return 0
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            print(line.rstrip())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="migtool", description="声明式数据迁移工具（仅标准库）")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init-demo", help="生成演示数据库")
    p.add_argument("store")
    p.add_argument("--records", type=int, default=20)
    p.add_argument("--seed", type=int, default=42)
    p.set_defaults(func=cmd_init_demo)

    p = sub.add_parser("check", help="兼容性评估（不执行）")
    p.add_argument("store")
    p.add_argument("spec")
    p.add_argument("--json", action="store_true", help="输出 JSON 报告")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("dry-run", help="在副本上预演，输出变更量与预估耗时")
    p.add_argument("store")
    p.add_argument("spec")
    p.add_argument("--sample", type=int, default=None, help="抽样记录数")
    p.add_argument("--batch-size", type=int, default=100)
    p.add_argument("--report", help="把 JSON 报告写入该文件")
    p.set_defaults(func=cmd_dry_run)

    p = sub.add_parser("run", help="执行迁移（有破坏性步骤时需 --yes）")
    p.add_argument("store")
    p.add_argument("spec")
    p.add_argument("--yes", action="store_true",
                   help="显式确认执行破坏性步骤")
    p.add_argument("--batch-size", type=int, default=100)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("rollback",
                       help="回滚：默认回滚最后一步，--to-step 回到指定步骤，"
                            "--all 全部回滚")
    p.add_argument("store")
    p.add_argument("spec")
    p.add_argument("--to-step")
    p.add_argument("--all", action="store_true")
    p.set_defaults(func=cmd_rollback)

    p = sub.add_parser("repair", help="前向修复：把中断的迁移推进到完成")
    p.add_argument("store")
    p.add_argument("spec")
    p.add_argument("--batch-size", type=int, default=100)
    p.set_defaults(func=cmd_repair)

    p = sub.add_parser("status", help="查看迁移状态")
    p.add_argument("store")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("audit", help="查看审计日志")
    p.add_argument("store")
    p.set_defaults(func=cmd_audit)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except SpecError as exc:
        print(f"迁移描述错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
