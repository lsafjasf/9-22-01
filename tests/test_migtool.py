"""migtool 自测：python -m unittest discover -s tests -v"""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from migtool import (Engine, NeedsConfirmation, Store, dry_run, parse_spec)

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_SPEC = REPO_ROOT / "examples" / "migration.json"


def make_spec(steps, **kw):
    return parse_spec({"name": "t", "from_version": 1, "to_version": 2,
                       "steps": steps, **kw})


class KillError(Exception):
    """模拟强杀。"""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="migtool-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = Store(self.tmp / "db")

    def seed(self, docs):
        for rid, doc in docs.items():
            self.store.write(rid, doc)

    def dump(self):
        return {rid: self.store.read(rid) for rid in self.store.list_ids()}


class TestSpec(Base):
    def test_topo_order_respects_dependencies(self):
        spec = make_spec([
            {"id": "c", "type": "add_field", "field": "c", "default": 1,
             "depends_on": ["a", "b"]},
            {"id": "b", "type": "add_field", "field": "b", "default": 1,
             "depends_on": ["a"]},
            {"id": "a", "type": "add_field", "field": "a", "default": 1},
        ])
        self.assertEqual([s.id for s in spec.steps], ["a", "b", "c"])

    def test_cycle_detected(self):
        from migtool import SpecError
        with self.assertRaises(SpecError):
            make_spec([
                {"id": "a", "type": "add_field", "field": "a", "default": 1,
                 "depends_on": ["b"]},
                {"id": "b", "type": "add_field", "field": "b", "default": 1,
                 "depends_on": ["a"]},
            ])

    def test_unknown_type_rejected(self):
        from migtool import SpecError
        with self.assertRaises(SpecError):
            make_spec([{"id": "x", "type": "nope"}])


class TestEvaluate(Base):
    def test_breaking_steps_flagged(self):
        from migtool import load_spec
        engine = Engine(self.store, load_spec(EXAMPLE_SPEC))
        report = engine.evaluate()
        self.assertEqual(report["overall"], "breaking")
        self.assertTrue(report["requires_confirmation"])
        levels = {s["id"]: s["level"] for s in report["steps"]}
        self.assertEqual(levels["split_name"], "breaking")
        self.assertEqual(levels["drop_legacy_tags"], "breaking")
        self.assertEqual(levels["age_to_int"], "breaking")
        self.assertEqual(levels["backfill_role"], "safe")
        self.assertEqual(levels["rebuild_status_index"], "safe")

    def test_run_refused_without_confirmation(self):
        spec = make_spec([{"id": "d", "type": "drop_field", "field": "x"}])
        self.seed({"r1": {"x": 1}})
        with self.assertRaises(NeedsConfirmation):
            Engine(self.store, spec).run()
        self.assertEqual(self.store.read("r1"), {"x": 1})  # 未被执行


class TestApply(Base):
    def test_all_step_types(self):
        spec = make_spec([
            {"id": "add", "type": "add_field", "field": "status",
             "default": "active"},
            {"id": "fill", "type": "backfill_default", "field": "role",
             "default": "user"},
            {"id": "typ", "type": "change_type", "field": "age",
             "from": "str", "to": "int"},
            {"id": "split", "type": "split_field", "field": "name",
             "into": ["first", "last"], "separator": " "},
            {"id": "merge", "type": "merge_fields", "fields": ["first", "last"],
             "into": "full", "separator": "|"},
            {"id": "drop", "type": "drop_field", "field": "junk"},
        ])
        self.seed({"r1": {"name": "Ada Lovelace", "age": "36", "junk": True}})
        Engine(self.store, spec).run(allow_breaking=True)
        self.assertEqual(self.store.read("r1"), {
            "status": "active", "role": "user", "age": 36,
            "full": "Ada|Lovelace",
        })

    def test_change_type_idempotent(self):
        spec = make_spec([{"id": "t", "type": "change_type", "field": "n",
                           "from": "str", "to": "int"}])
        self.seed({"r1": {"n": "7"}})
        engine = Engine(self.store, spec)
        engine.run(allow_breaking=True)
        engine.run(allow_breaking=True)  # 重复执行无副作用
        self.assertEqual(self.store.read("r1"), {"n": 7})

    def test_rebuild_index(self):
        spec = make_spec([
            {"id": "add", "type": "add_field", "field": "status",
             "default": "active"},
            {"id": "idx", "type": "rebuild_index", "index": "by_status",
             "field": "status", "depends_on": ["add"]},
        ])
        self.seed({"r1": {}, "r2": {}})
        Engine(self.store, spec).run(allow_breaking=True)
        index = self.store.read_index("by_status")
        self.assertEqual(index["map"], {"active": ["r1", "r2"]})


class TestInterruptResume(Base):
    def test_kill_mid_step_then_resume_no_duplicate_effects(self):
        docs = {f"r{i}": {"a": f"x{i}", "b": f"y{i}"} for i in range(5)}
        self.seed(docs)
        spec = make_spec([
            {"id": "merge", "type": "merge_fields", "fields": ["a", "b"],
             "into": "ab", "separator": "-"},
        ])
        calls = {"n": 0}

        def killer(step_id, batch_no):
            calls["n"] += 1
            if calls["n"] == 1:
                raise KillError("模拟强杀")

        engine = Engine(self.store, spec, batch_size=2, on_batch=killer)
        with self.assertRaises(KillError):
            engine.run(allow_breaking=True)

        # 重启（新 Engine 实例），从中断处继续
        outcome = Engine(self.store, spec, batch_size=2).run(allow_breaking=True)
        for i in range(5):
            self.assertEqual(self.store.read(f"r{i}"), {"ab": f"x{i}-y{i}"})
        # 审计里能看到中断前的批次提交与重启后的继续
        audit = (self.store.meta_dir / "audit.jsonl").read_text()
        self.assertIn("batch_committed", audit)
        self.assertIn("migration_started", audit)
        self.assertEqual(len(outcome["results"]), 1)

    def test_kill_between_snapshot_and_write_is_consistent(self):
        """快照已写、记录未写的崩溃点：重启后幂等重放，回滚仍精确。"""
        self.seed({f"r{i}": {"v": i} for i in range(4)})
        spec = make_spec([{"id": "t", "type": "change_type", "field": "v",
                           "from": "int", "to": "str"}])
        original = self.dump()
        calls = {"n": 0}

        def killer(step_id, batch_no):
            calls["n"] += 1
            raise KillError

        with self.assertRaises(KillError):
            Engine(self.store, spec, batch_size=1, on_batch=killer).run(
                allow_breaking=True)
        Engine(self.store, spec, batch_size=1).run(allow_breaking=True)
        self.assertEqual(self.store.read("r0"), {"v": "0"})
        # 回滚后必须与迁移前完全一致
        Engine(self.store, spec).rollback(all_steps=True)
        self.assertEqual(self.dump(), original)

    def test_repair_completes_interrupted_migration(self):
        self.seed({f"r{i}": {"x": 1} for i in range(3)})
        spec = make_spec([
            {"id": "a", "type": "add_field", "field": "s", "default": "on"},
            {"id": "b", "type": "drop_field", "field": "x",
             "depends_on": ["a"]},
        ])
        calls = {"n": 0}

        def killer(step_id, batch_no):
            if step_id == "b":
                calls["n"] += 1
                if calls["n"] == 1:
                    raise KillError

        with self.assertRaises(KillError):
            Engine(self.store, spec, batch_size=1, on_batch=killer).run(
                allow_breaking=True)
        outcome = Engine(self.store, spec, batch_size=1).repair()
        self.assertEqual([r["id"] for r in outcome["results"]], ["b"])
        for i in range(3):
            self.assertEqual(self.store.read(f"r{i}"), {"s": "on"})


class TestRollback(Base):
    def _example_docs(self):
        return {
            "r1": {"id": "r1", "name": "Ada Lovelace", "age": "36",
                   "email": "ada@x.com", "tags": ["vip"]},
            "r2": {"id": "r2", "name": "Bob Smith", "age": "41",
                   "email": "bob@x.com", "tags": ["trial", "beta"]},
            "r3": {"id": "r3", "name": "Carol", "age": "29",
                   "email": "carol@x.com", "tags": []},
        }

    def test_rollback_all_restores_exact_semantics(self):
        from migtool import load_spec
        self.seed(self._example_docs())
        original = self.dump()
        spec = load_spec(EXAMPLE_SPEC)
        Engine(self.store, spec).run(allow_breaking=True)
        self.assertNotEqual(self.dump(), original)  # 迁移确实生效
        Engine(self.store, spec).rollback(all_steps=True)
        self.assertEqual(self.dump(), original)     # 回滚后完全一致
        self.assertFalse(self.store.index_exists("by_status"))

    def test_rollback_to_step(self):
        spec = make_spec([
            {"id": "a", "type": "add_field", "field": "f1", "default": 1},
            {"id": "b", "type": "add_field", "field": "f2", "default": 2,
             "depends_on": ["a"]},
            {"id": "c", "type": "add_field", "field": "f3", "default": 3,
             "depends_on": ["b"]},
        ])
        self.seed({"r1": {"v": 0}})
        engine = Engine(self.store, spec)
        engine.run(allow_breaking=True)
        engine.rollback(to_step="a")  # 回滚 b、c，保留 a
        self.assertEqual(self.store.read("r1"), {"v": 0, "f1": 1})

    def test_backfill_rollback_only_touches_filled_records(self):
        spec = make_spec([{"id": "fill", "type": "backfill_default",
                           "field": "role", "default": "user"}])
        self.seed({"r1": {}, "r2": {"role": "admin"}})
        engine = Engine(self.store, spec)
        engine.run(allow_breaking=True)
        self.assertEqual(self.store.read("r1"), {"role": "user"})
        engine.rollback(all_steps=True)
        self.assertEqual(self.store.read("r1"), {})  # 只撤销被回填的记录
        self.assertEqual(self.store.read("r2"), {"role": "admin"})

    def test_audit_trail_complete(self):
        from migtool import load_spec
        self.seed(self._example_docs())
        spec = load_spec(EXAMPLE_SPEC)
        Engine(self.store, spec).run(allow_breaking=True)
        Engine(self.store, spec).rollback(all_steps=True)
        events = [json.loads(line)["event"] for line in
                  (self.store.meta_dir / "audit.jsonl").read_text().splitlines()]
        for expected in ("migration_started", "step_started", "step_done",
                         "migration_done", "rollback_started",
                         "step_rolled_back", "rollback_done"):
            self.assertIn(expected, events)


class TestDryRun(Base):
    def test_dry_run_does_not_touch_store_and_reports(self):
        from migtool import load_spec
        self.seed({f"r{i}": {"name": f"A{i} B{i}", "age": str(i),
                             "tags": []} for i in range(10)})
        before = self.dump()
        report = dry_run(self.store, load_spec(EXAMPLE_SPEC))
        self.assertEqual(self.dump(), before)          # 原库未被改动
        self.assertEqual(self.store.count(), 10)
        self.assertFalse((self.store.meta_dir / "state.json").exists())
        self.assertEqual(report["records_rehearsed"], 10)
        self.assertGreater(report["changed_total"], 0)
        self.assertIn("estimated_seconds_full", report)
        self.assertEqual(report["compatibility"]["overall"], "breaking")

    def test_dry_run_sample_extrapolates(self):
        spec = make_spec([{"id": "a", "type": "add_field", "field": "s",
                           "default": 1}])
        self.seed({f"r{i}": {} for i in range(20)})
        report = dry_run(self.store, spec, sample=5)
        self.assertEqual(report["records_rehearsed"], 5)
        self.assertEqual(report["changed_total"], 5)
        self.assertEqual(report["changed_total_estimated"], 20)


class TestCli(Base):
    def run_cli(self, *argv):
        return subprocess.run(
            [sys.executable, "-m", "migtool", *argv],
            cwd=REPO_ROOT, capture_output=True, text=True)

    def test_cli_end_to_end(self):
        store = self.tmp / "cli-db"
        r = self.run_cli("init-demo", str(store), "--records", "8")
        self.assertEqual(r.returncode, 0, r.stderr)

        r = self.run_cli("check", str(store), str(EXAMPLE_SPEC))
        self.assertEqual(r.returncode, 2)  # 有破坏性步骤
        self.assertIn("[破坏]", r.stdout)

        r = self.run_cli("run", str(store), str(EXAMPLE_SPEC))
        self.assertEqual(r.returncode, 2)  # 未确认，拒绝执行

        report_path = self.tmp / "report.json"
        r = self.run_cli("dry-run", str(store), str(EXAMPLE_SPEC),
                         "--report", str(report_path))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(report_path.exists())

        r = self.run_cli("run", str(store), str(EXAMPLE_SPEC), "--yes")
        self.assertEqual(r.returncode, 0, r.stderr)

        r = self.run_cli("status", str(store))
        self.assertEqual(r.returncode, 0)
        self.assertIn("split_name", r.stdout)

        r = self.run_cli("rollback", str(store), str(EXAMPLE_SPEC), "--all")
        self.assertEqual(r.returncode, 0, r.stderr)

        r = self.run_cli("audit", str(store))
        self.assertIn("rollback_done", r.stdout)


if __name__ == "__main__":
    unittest.main()
