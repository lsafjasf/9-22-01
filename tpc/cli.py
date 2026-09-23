"""Command line interface.

  python -m tpc run     --dir D --txn T1 --parts p1,p2,p3 [fault options]
  python -m tpc recover --dir D            # restart nodes from logs, finish
  python -m tpc blocked --dir D            # list blocked (prepared, undecided) txns
  python -m tpc doctor  --dir D            # read-only recovery report
"""
import argparse
import os
import sys

from .coordinator import Coordinator
from .doctor import analyze, format_report
from .logstore import LogCorruptError
from .participant import Participant
from .sim import Faults, Sim

COORD = "coord"


def _build_sim(args):
    faults = Faults(drop=args.drop, dup=args.dup,
                    min_delay=0.0, max_delay=args.max_delay)
    return Sim(seed=args.seed, faults=faults)


def _load_participants(dir, votes=None):
    votes = votes or {}
    parts = []
    for fname in sorted(os.listdir(dir)):
        if fname.endswith(".log") and fname[:-4] != COORD:
            name = fname[:-4]
            parts.append(Participant(name, dir, coordinator=COORD,
                                     vote=votes.get(name, "yes")))
    return parts


def cmd_run(args):
    os.makedirs(args.dir, exist_ok=True)
    sim = _build_sim(args)
    votes = {}
    for spec in args.no or []:
        votes[spec] = "no"
    coord = Coordinator(COORD, args.dir, vote_timeout=args.vote_timeout)
    parts = [Participant(p, args.dir, coordinator=COORD,
                         vote=votes.get(p, "yes")) for p in args.parts.split(",")]
    coord.start(sim)
    for p in parts:
        p.start(sim)
    coord.begin(sim, args.txn, [p.name for p in parts])

    crashes = []
    for spec in args.crash or []:
        name, span = spec.split("@")
        t1, t2 = span.split(":")
        crashes.append((float(t1), float(t2), name))
    crashed = {}

    while sim.time < args.max_time:
        for t1, t2, name in list(crashes):
            node = coord if name == COORD else next(
                (p for p in parts if p.name == name), None)
            if node is None:
                continue
            if name not in crashed and sim.time >= t1:
                node.crash()
                crashed[name] = True
                print("[t=%.1f] %s 被强杀 (crash)" % (sim.time, name))
            elif crashed.get(name) and sim.time >= t2:
                node.restart(sim)
                crashed[name] = False
                print("[t=%.1f] %s 重启并从日志恢复" % (sim.time, name))
        if not sim.step():
            break

    st = coord.txns.get(args.txn)
    print("协调者决议: %s" % (st.decision if st else "?"))
    for p in parts:
        print("参与方 %-6s 状态: %s  阻塞: %s"
              % (p.name, p.decision_of(args.txn) or "未决", p.blocked() or "无"))
    print("消息: 投递 %d, 丢弃 %d" % (sim.delivered, sim.dropped))


def cmd_recover(args):
    sim = _build_sim(args)
    coord = Coordinator(COORD, args.dir, vote_timeout=args.vote_timeout)
    parts = _load_participants(args.dir)
    try:
        coord.start(sim)
        for p in parts:
            p.start(sim)
    except LogCorruptError as e:
        print("日志损坏,自动恢复已拒绝猜测: %s" % e, file=sys.stderr)
        print("请运行: python -m tpc doctor --dir %s" % args.dir, file=sys.stderr)
        return 2
    sim.run(args.max_time)
    for txn, st in sorted(coord.txns.items()):
        print("事务 %s: 协调者决议=%s done=%s" % (txn, st.decision, st.done))
        for p in parts:
            print("  %-6s -> %s" % (p.name, p.decision_of(txn) or "未决(阻塞)"))
    return 0


def cmd_blocked(args):
    found = False
    for p in _load_participants(args.dir):
        p._replay()
        if p.blocked():
            found = True
            for txn, vote in p.blocked().items():
                print("%s: 事务 %s 阻塞中 (已投票 %s, 未收到决议)" % (p.name, txn, vote))
    if not found:
        print("无阻塞事务。")


def cmd_doctor(args):
    print(format_report(analyze(args.dir)))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="tpc", description="两阶段提交 (2PC) 工具")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--dir", required=True, help="节点日志目录")
        p.add_argument("--seed", type=int, default=1)
        p.add_argument("--drop", type=float, default=0.0, help="丢包概率")
        p.add_argument("--dup", type=float, default=0.0, help="重复概率")
        p.add_argument("--max-delay", type=float, default=0.0, help="最大延迟")
        p.add_argument("--vote-timeout", type=float, default=5.0)
        p.add_argument("--max-time", type=float, default=60.0)

    p = sub.add_parser("run", help="运行一次事务(可注入故障)")
    common(p)
    p.add_argument("--txn", default="T1")
    p.add_argument("--parts", default="p1,p2,p3")
    p.add_argument("--no", action="append", help="投 no 的参与方,可多次")
    p.add_argument("--crash", action="append",
                   help="如 coord@2:6 表示 t=2 强杀, t=6 重启,可多次")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("recover", help="从日志重启所有节点并完成事务")
    common(p)
    p.set_defaults(fn=cmd_recover)

    p = sub.add_parser("blocked", help="查询各参与方阻塞清单")
    p.add_argument("--dir", required=True)
    p.set_defaults(fn=cmd_blocked)

    p = sub.add_parser("doctor", help="只读恢复报告(日志损坏时使用)")
    p.add_argument("--dir", required=True)
    p.set_defaults(fn=cmd_doctor)

    args = ap.parse_args(argv)
    return args.fn(args) or 0
