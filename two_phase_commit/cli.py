"""Command line interface: demos, the read-only recovery tool, self-tests.

Examples
--------
Run every fault-injection test::

    python3 -m two_phase_commit.cli selftest

Run a named demo and keep its durable logs for inspection::

    python3 -m two_phase_commit.cli demo chaos --seed 7 --keep-logs --datadir runs/7

Inspect (read-only) a possibly damaged deployment::

    python3 -m two_phase_commit.cli inspect runs/7
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import unittest

from . import forensics
from .coordinator import Coordinator
from .participant import Participant
from .protocol import Msg
from .sim import Simulator, SimNetwork
from .storage import DurableLog, corrupt_tail

COORD = "coordinator"
SCENARIOS = ("happy", "vote-no", "kill-coordinator", "kill-participant",
             "partition", "durability-order", "chaos")


# ---------------------------------------------------------------------------
# Demo harness
# ---------------------------------------------------------------------------

class Deployment:
    """Builds coordinator + participants over a Simulator and a log dir."""

    def __init__(self, datadir, n_participants=3, network=None,
                 no_voters=(), keep_effects=None):
        self.datadir = datadir
        os.makedirs(datadir, exist_ok=True)
        self.network = network or SimNetwork()
        self.n_participants = n_participants
        self.no_voters = set(no_voters)
        self.keep_effects = keep_effects if keep_effects is not None else set()
        self.participant_ids = ["p%d" % i for i in range(n_participants)]
        self.events = []
        self.sim = Simulator(self.network, self._factory)
        self._hooks = {}  # node_id -> hook config

    def log_path(self, node_id):
        if node_id == COORD:
            return os.path.join(self.datadir, "coordinator.log")
        return os.path.join(self.datadir, "participant-%s.log" % node_id)

    def _effect_dir(self, node_id):
        d = os.path.join(self.datadir, "effects", node_id)
        os.makedirs(d, exist_ok=True)
        return d

    def effect_committed(self, node_id, txn):
        return os.path.exists(os.path.join(self._effect_dir(node_id), txn + ".commit"))

    def effect_count(self):
        total = 0
        for pid in self.participant_ids:
            d = os.path.join(self.datadir, "effects", pid)
            if os.path.isdir(d):
                total += len([f for f in os.listdir(d) if f.endswith(".commit")])
        return total

    def _factory(self, node_id):
        if node_id == COORD:
            log = DurableLog(self.log_path(COORD))
            return Coordinator(
                node_id, log, self.participant_ids,
                decision_hook=self._hooks.get("coord_decision"),
            )
        log = DurableLog(self.log_path(node_id))
        keep = node_id in self.keep_effects

        def vote_fn(txn, _n=node_id):
            return _n not in self.no_voters

        def commit_fn(txn, _n=node_id):
            d = self._effect_dir(_n)
            path = os.path.join(d, txn + ".commit")
            # Idempotent durable resource effect: writing the marker twice
            # does not double-apply.
            with open(path, "a") as fh:
                fh.write("commit at %r\n" % self.sim.time)

        def abort_fn(txn, _n=node_id):
            d = self._effect_dir(_n)
            with open(os.path.join(d, txn + ".abort"), "a") as fh:
                fh.write("abort at %r\n" % self.sim.time)

        return Participant(
            node_id, log, coordinator_id=COORD,
            vote_fn=vote_fn, commit_fn=commit_fn, abort_fn=abort_fn,
            prepared_hook=self._hooks.get(("prepared", node_id)),
            applied_hook=self._hooks.get(("applied", node_id)),
        )

    def set_hook(self, key, fn):
        self._hooks[key] = fn

    def start_all(self):
        self.sim.boot(COORD)
        for pid in self.participant_ids:
            self.sim.boot(pid)

    def begin(self, txn, at=0.1):
        self.sim.call(COORD, at, lambda node: node.begin_txn(txn))

    def kill(self, node_id, at=None):
        if at is None:
            self.sim.kill(node_id)
        else:
            self.sim.kill_at(node_id, at)

    def boot(self, node_id, at):
        self.sim.boot_at(node_id, at=at)

    def partition(self, a, b, at):
        self.sim.schedule(at, ("partition", a, b))

    def heal(self, a, b, at):
        self.sim.schedule(at, ("heal", a, b))

    def run(self, budget=30.0):
        self._apply_network_events(budget)
        self.sim.run_until(self.sim.time + budget)

    def _apply_network_events(self, budget):
        import heapq
        events = self._drain()
        for at, seq, event in events:
            if event[0] == "partition":
                self.network.partition(event[1], event[2])
            elif event[0] == "heal":
                self.network.heal(event[1], event[2])
            else:
                heapq.heappush(self.sim._events, (at, seq, event))

    def _drain(self):
        import heapq
        out = []
        while self.sim._events:
            out.append(heapq.heappop(self.sim._events))
        return out

    # -- outcome reading (from durable logs, the only truth after crashes) --

    def outcome_from_logs(self, txn):
        commits, aborts, blocked = [], [], []
        for pid in self.participant_ids:
            view = forensics.inspect_node(pid, self.log_path(pid), "participant")
            if view.has("APPLY", txn=txn, outcome="COMMIT"):
                commits.append(pid)
            elif view.has("APPLY", txn=txn, outcome="ABORT"):
                aborts.append(pid)
            elif view.has("VOTE", txn=txn, vote="VOTE_COMMIT"):
                blocked.append(pid)
        return commits, aborts, blocked

    def coordinator_decision(self, txn):
        view = forensics.inspect_node(COORD, self.log_path(COORD), "coordinator")
        decs = view.payloads("DECISION", txn=txn)
        return (decs[-1]["decision"] if decs else None, view)


def build_scenario(name, datadir, seed=0, n=3, txn="T1", verbose=True):
    net_kwargs = {"seed": seed}
    no_voters = ()
    keep = set()
    if name == "happy":
        net_kwargs.update(latency=0.01, jitter=0.02)
    elif name == "vote-no":
        no_voters = ("p1",)
    elif name == "kill-coordinator":
        net_kwargs.update(loss=0.2, duplicate=0.1, seed=seed)
    elif name == "kill-participant":
        net_kwargs.update(duplicate=0.3, reorder=0.4)
        keep.add("p2")
    elif name == "partition":
        net_kwargs.update(loss=0.1)
    elif name == "durability-order":
        keep.update(["p0", "p1", "p2"])
    elif name == "chaos":
        net_kwargs.update(loss=0.15, duplicate=0.15, reorder=0.2)
        keep.update(["p0", "p1", "p2"])
    else:
        raise ValueError(name)

    dep = Deployment(datadir, n_participants=n,
                     network=SimNetwork(**net_kwargs),
                     no_voters=no_voters, keep_effects=keep)

    # Kill the coordinator at the most dangerous instant: right after the
    # COMMIT record is fsync'd but before any decision message is sent.
    if name in ("kill-coordinator", "durability-order", "chaos"):
        killed = {"v": False}

        def coord_hook(t, decision, _k=killed):
            if not _k["v"]:
                _k["v"] = True
                dep.sim.kill(COORD)
        dep.set_hook("coord_decision", coord_hook)
    if name == "kill-participant":
        def applied_hook(t, outcome, _dep=dep):
            if outcome == "COMMIT":
                _dep.sim.kill("p2")
        dep.set_hook(("applied", "p2"), applied_hook)

    dep.start_all()
    dep.begin(txn, at=0.2)

    if name in ("kill-coordinator", "durability-order", "chaos"):
        dep.boot(COORD, 1.5)
    if name == "kill-participant":
        dep.boot("p2", 1.2)
    if name == "partition":
        dep.partition(COORD, "p2", 0.25)
        dep.heal(COORD, "p2", 2.0)
    if name == "chaos":
        # Participant crashes at awkward moments, then restarts.
        dep.kill("p0", 0.55)
        dep.boot("p0", 1.8)
        dep.kill("p1", 2.4)
        dep.boot("p1", 3.2)
        dep.partition(COORD, "p2", 0.8)
        dep.heal(COORD, "p2", 2.6)
    return dep


def summarize(dep, txn="T1"):
    decision, cview = dep.coordinator_decision(txn)
    commits, aborts, blocked = dep.outcome_from_logs(txn)
    consistent = not (commits and aborts)
    return {
        "coordinator_decision": decision,
        "committed": commits,
        "aborted": aborts,
        "blocked": blocked,
        "consistent": consistent,
        "committed_effects_on_disk": dep.effect_count(),
        "network_stats": dep.network.stats.as_dict(),
        "end_time": round(dep.sim.time, 3),
        "coordinator_log_corrupt": cview.corruption is not None,
    }


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_demo(args):
    datadir = args.datadir
    created_temp = False
    if datadir is None:
        datadir = tempfile.mkdtemp(prefix="tpc-")
        created_temp = True
    dep = build_scenario(args.scenario, datadir, seed=args.seed,
                         n=args.participants, verbose=not args.json)
    dep.run(budget=args.budget)
    result = summarize(dep)
    if args.json:
        print(json.dumps({"datadir": datadir, **result}, indent=2, sort_keys=True))
    else:
        print("scenario      : %s (seed=%d, participants=%d)"
              % (args.scenario, args.seed, args.participants))
        print("data dir      : %s" % datadir)
        print("coord decision: %s" % result["coordinator_decision"])
        print("committed     : %s" % result["committed"])
        print("aborted       : %s" % result["aborted"])
        print("blocked       : %s" % result["blocked"])
        print("consistent    : %s" % result["consistent"])
        print("commit effects: %d" % result["committed_effects_on_disk"])
        print("network       : sent=%(sent)d delivered=%(delivered)d "
              "dropped=%(dropped)d duplicated=%(duplicated)d "
              "reordered=%(reordered)d" % result["network_stats"])
    if not args.keep_logs and created_temp:
        shutil.rmtree(datadir, ignore_errors=True)
    elif not args.json:
        print("logs retained in: %s" % datadir)
    return 0 if result["consistent"] and not result["blocked"] else (
        0 if result["consistent"] else 2)


def cmd_inspect(args):
    reports = forensics.inspect(args.paths, txns=args.txns or None)
    if args.json:
        print(json.dumps([r.as_dict() for r in reports], indent=2, sort_keys=True))
    else:
        for r in reports:
            print("=" * 72)
            print("transaction %s" % r.txn)
            print("recommendation: %s" % r.recommendation)
            print("safe to proceed automatically: %s" % r.safe)
            print("evidence:")
            for f in r.findings:
                print("  - %s" % f)
            print("blocked participants : %s" % r.blocked_participants)
            print("committed participants: %s" % r.committed_participants)
            print("aborted participants : %s" % r.aborted_participants)
            print("manual steps:")
            for a in r.actions:
                print("  * %s" % a)
    # Exit code 0 = safe determination, 3 = ambiguous / insufficient.
    if all(r.safe for r in reports) and reports:
        return 0
    return 3


def cmd_selftest(args):
    loader = unittest.TestLoader()
    start_dir = os.path.join(os.path.dirname(__file__), "tests")
    suite = loader.discover(start_dir, pattern="test_*.py",
                            top_level_dir=os.path.dirname(os.path.dirname(__file__)))
    runner = unittest.TextTestRunner(verbosity=args.verbosity)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


def main(argv=None):
    parser = argparse.ArgumentParser(prog="two_phase_commit")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_demo = sub.add_parser("demo", help="run a fault-injection scenario")
    p_demo.add_argument("scenario", choices=SCENARIOS)
    p_demo.add_argument("--seed", type=int, default=1)
    p_demo.add_argument("--participants", type=int, default=3)
    p_demo.add_argument("--datadir", default=None)
    p_demo.add_argument("--keep-logs", action="store_true")
    p_demo.add_argument("--json", action="store_true")
    p_demo.add_argument("--budget", type=float, default=30.0)
    p_demo.set_defaults(func=cmd_demo)

    p_ins = sub.add_parser("inspect", help="read-only recovery / forensics tool")
    p_ins.add_argument("paths", nargs="+")
    p_ins.add_argument("--txns", nargs="*")
    p_ins.add_argument("--json", action="store_true")
    p_ins.set_defaults(func=cmd_inspect)

    p_test = sub.add_parser("selftest", help="run all fault-injection tests")
    p_test.add_argument("-v", "--verbosity", type=int, default=2)
    p_test.set_defaults(func=cmd_selftest)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
