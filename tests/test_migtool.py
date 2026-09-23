"""自测：依赖排序、兼容评估、执行正确性、强杀重入、回滚保真、预演报告。"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from migtool import Engine, SimulatedKill, dry_run, evaluate, load_migration
from migtool.cli import main as cli_main
from migtool.compat import BREAKING, RISKY, SAFE
from migtool.model import MigrationError, topo_sort, Step

MIGRATION = {
    "migration_id": "m_test",
    "table": "users",
    "batch_size": 10,
    "steps": [
        {"id": "add_email", "type": "add_field", "field": "email",
         "field_type": "TEXT", "default": "", "backfill": True},
        {"id": "split_name", "type": "split_field", "source": "full_name",
         "targets": ["first_name", "last_name"], "delimiter": " ",
         "keep_source": True},
        {"id": "age_int", "type": "change_type", "field": "age",
         "from": "TEXT", "to": "INTEGER",
         "conversion": {"kind": "cast", "to": "INTEGER", "on_error": "null"}},
        {"id": "fill_status", "type": "backfill_default",
         "field": "status", "value": "inactive"},
        {"id": "merge_addr", "type": "merge_fields", "sources": ["city", "street"],
         "target": "address", "separator": " ", "keep_sources": True,
         "depends_on": ["split_name"]},
        {"id": "drop_legacy", "type": "drop_field", "field": "legacy_flag",
         "depends_on": ["fill_status"]},
        {"id": "idx_email", "type": "rebuild_index", "index": "idx_users_email",
         "fields": ["email"], "depends_on": ["add_email"]},
    ],
}


def make_db(path, rows=95):
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE users(
        id INTEGER PRIMARY KEY, full_name TEXT, age TEXT, city TEXT,
        street TEXT, status TEXT, legacy_flag TEXT)""")
    data = [(f"Zhang San {i}", str(20 + i % 40), f"City{i%5}", f"St{i}",
             None if i % 3 else "active", f"flag{i}") for i in range(rows)]
    conn.executemany(
        "INSERT INTO users(full_name,age,city,street,status,legacy_flag) "
        "VALUES(?,?,?,?,?,?)", data)
    conn.commit()
    conn.close()


def fingerprint(path, table="users"):
    conn = sqlite3.connect(path)
    cols = sorted(r[1] for r in conn.execute(f"PRAGMA table_info({table})"))
    col_list = ", ".join(f'"{c}"' for c in cols)
    rows = conn.execute(f"SELECT {col_list} FROM {table} ORDER BY rowid").fetchall()
    conn.close()
    return cols, rows


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, "t.db")
        make_db(self.db)
        self.mig_path = os.path.join(self.dir, "m.json")
        with open(self.mig_path, "w") as f:
            json.dump(MIGRATION, f)
        self.mig = load_migration(self.mig_path)


class TestModel(Base):
    def test_topo_order_respects_deps(self):
        ordered = [s.id for s in self.mig.ordered_steps()]
        self.assertLess(ordered.index("split_name"), ordered.index("merge_addr"))
        self.assertLess(ordered.index("fill_status"), ordered.index("drop_legacy"))
        self.assertLess(ordered.index("add_email"), ordered.index("idx_email"))

    def test_cycle_detected(self):
        a = Step("a", "add_field", {"field": "x", "field_type": "TEXT"}, ["b"])
        b = Step("b", "add_field", {"field": "y", "field_type": "TEXT"}, ["a"])
        with self.assertRaises(MigrationError):
            topo_sort([a, b])

    def test_missing_dep_detected(self):
        a = Step("a", "add_field", {"field": "x", "field_type": "TEXT"}, ["nope"])
        with self.assertRaises(MigrationError):
            topo_sort([a])


class TestCompat(Base):
    def test_levels(self):
        findings = {f.step_id: f for f in evaluate(self.mig)}
        self.assertEqual(findings["add_email"].level, SAFE)
        self.assertEqual(findings["split_name"].level, RISKY)
        self.assertEqual(findings["age_int"].level, BREAKING)
        self.assertEqual(findings["drop_legacy"].level, BREAKING)
        self.assertEqual(findings["idx_email"].level, SAFE)
        self.assertTrue(findings["drop_legacy"].reason)

    def test_gate_blocks_breaking_without_confirm(self):
        rc = cli_main(["apply", "--db", self.db, "--migration", self.mig_path])
        self.assertEqual(rc, 1)
        cols = [r[1] for r in sqlite3.connect(self.db).execute(
            "PRAGMA table_info(users)")]
        self.assertNotIn("email", cols)  # 未执行任何副作用

    def test_gate_allows_with_confirm(self):
        rc = cli_main(["apply", "--db", self.db, "--migration", self.mig_path,
                       "--confirm-breaking"])
        self.assertEqual(rc, 0)


class TestApply(Base):
    def test_apply_all_steps(self):
        eng = Engine(self.db, self.mig)
        eng.apply()
        eng.close()
        conn = sqlite3.connect(self.db)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(users)")]
        for c in ("email", "first_name", "last_name", "address"):
            self.assertIn(c, cols)
        self.assertNotIn("legacy_flag", cols)
        row = conn.execute(
            "SELECT first_name, last_name, age, typeof(age), status, address, email "
            "FROM users WHERE id=1").fetchone()
        self.assertEqual(row[0:2], ("Zhang".title(), "San 0"))
        self.assertEqual(row[3], "integer")
        self.assertEqual(row[4], "active")
        self.assertEqual(row[5], "City0 St0")
        self.assertEqual(row[6], "")
        # 回填只影响 NULL 行
        n = conn.execute(
            "SELECT COUNT(*) FROM users WHERE status='inactive'").fetchone()[0]
        self.assertEqual(n, sum(1 for i in range(95) if i % 3 != 0))
        idx = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_users_email'").fetchone()
        self.assertIsNotNone(idx)
        conn.close()

    def test_audit_written(self):
        eng = Engine(self.db, self.mig)
        eng.apply()
        events = [r["event"] for r in eng.audit_rows()]
        eng.close()
        self.assertIn("migration_start", events)
        self.assertIn("migration_done", events)
        self.assertEqual(events.count("step_done"), len(self.mig.steps))


class TestKillResume(Base):
    def test_resume_after_kill_no_duplicate_effects(self):
        calls = {"n": 0}

        def hook():
            calls["n"] += 1
            if calls["n"] == 7:  # 第 7 批提交后模拟强杀
                raise SimulatedKill()

        eng = Engine(self.db, self.mig, fault_hook=hook)
        with self.assertRaises(SimulatedKill):
            eng.apply()
        eng.close()

        # 重启续跑（同一进程模拟重启：新连接、无内存状态）
        eng2 = Engine(self.db, self.mig)
        eng2.apply()
        eng2.close()

        conn = sqlite3.connect(self.db)
        # 拆分结果精确（若重放会产生不一致则此处失败）
        bad = conn.execute(
            "SELECT COUNT(*) FROM users WHERE first_name IS NULL "
            "OR last_name IS NULL").fetchone()[0]
        self.assertEqual(bad, 0)
        # 回填幂等：inactive 行数精确等于原 NULL 行数
        n = conn.execute(
            "SELECT COUNT(*) FROM users WHERE status='inactive'").fetchone()[0]
        self.assertEqual(n, sum(1 for i in range(95) if i % 3 != 0))
        # age 全部完成转换
        t = conn.execute(
            "SELECT COUNT(*) FROM users WHERE typeof(age) != 'integer'").fetchone()[0]
        self.assertEqual(t, 0)
        conn.close()

    def test_kill_at_every_point(self):
        """在迁移的不同批次点强杀，每次都能续跑到一致终态。"""
        expected = None
        for kill_at in (1, 3, 9, 20, 60):
            db = os.path.join(self.dir, f"k{kill_at}.db")
            make_db(db)
            calls = {"n": 0}

            def hook():
                calls["n"] += 1
                if calls["n"] == kill_at:
                    raise SimulatedKill()

            eng = Engine(db, self.mig, fault_hook=hook)
            with self.assertRaises(SimulatedKill):
                eng.apply()
            eng.close()
            eng = Engine(db, self.mig)
            eng.apply()
            eng.close()
            fp = fingerprint(db)
            if expected is None:
                expected = fp
            self.assertEqual(fp, expected, f"kill_at={kill_at} 终态不一致")


class TestRollback(Base):
    def test_rollback_restores_exact_semantics(self):
        before = fingerprint(self.db)
        eng = Engine(self.db, self.mig)
        eng.apply()
        eng.rollback()
        eng.close()
        after = fingerprint(self.db)
        self.assertEqual(before, after)

    def test_rollback_is_reentrant(self):
        before = fingerprint(self.db)
        eng = Engine(self.db, self.mig)
        eng.apply()
        eng.close()
        calls = {"n": 0}

        def hook():
            calls["n"] += 1
            if calls["n"] == 5:
                raise SimulatedKill()

        eng = Engine(self.db, self.mig, fault_hook=hook)
        with self.assertRaises(SimulatedKill):
            eng.rollback()
        eng.close()
        eng = Engine(self.db, self.mig)
        eng.rollback()  # 续跑回滚
        eng.rollback()  # 重复回滚应为空操作
        eng.close()
        self.assertEqual(fingerprint(self.db), before)


class TestDryRun(Base):
    def test_report_fields_and_exact_fidelity(self):
        report = dry_run(self.db, self.mig, verify_rollback=True)
        self.assertEqual(report["rollback_fidelity"], "exact")
        self.assertEqual(report["production_rows"], 95)
        self.assertEqual(len(report["steps"]), len(self.mig.steps))
        for s in report["steps"]:
            self.assertIn("affected_rows", s)
            self.assertIn("estimated_full_ms", s)
        # 预演不得改动原库
        cols = [r[1] for r in sqlite3.connect(self.db).execute(
            "PRAGMA table_info(users)")]
        self.assertNotIn("email", cols)

    def test_sampled_dry_run_extrapolates(self):
        report = dry_run(self.db, self.mig, sample_rows=20, verify_rollback=False)
        self.assertEqual(report["replica_rows"], 20)
        self.assertEqual(report["production_rows"], 95)
        self.assertGreater(report["total_estimated_ms"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
