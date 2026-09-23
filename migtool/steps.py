"""各迁移步骤的前向/回滚实现。所有 DDL 幂等，所有数据变更分块且可续跑。"""
from __future__ import annotations

from .engine import Engine, convert_value, split_value
from .model import Step


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


# ---------- 公共原语 ----------

def _ensure_backup_table(eng: Engine, step: Step) -> str:
    bak = eng.backup_table(step)
    eng.conn.execute(
        f"CREATE TABLE IF NOT EXISTS {_q(bak)} (rowid INTEGER PRIMARY KEY, value)")
    eng.conn.commit()
    return bak


def _backup_column(eng: Engine, step: Step, field: str, direction: str = "forward") -> None:
    """把某列当前值分块复制到备份表（INSERT OR IGNORE，幂等）。"""
    bak = _ensure_backup_table(eng, step)
    t = _q(eng.migration.table)

    def fetch(cursor, limit):
        return eng.conn.execute(
            f"SELECT rowid AS rid, {_q(field)} AS v FROM {t} WHERE rowid > ? "
            f"AND rowid NOT IN (SELECT rowid AS rid FROM {_q(bak)}) "
            f"ORDER BY rowid LIMIT ?", (cursor, limit)).fetchall()

    def apply(rows):
        eng.conn.executemany(
            f"INSERT OR IGNORE INTO {_q(bak)}(rowid, value) VALUES(?, ?)",
            [(r["rid"], r["v"]) for r in rows])

    eng.run_chunked(step, direction, f"backup_{field}", fetch, apply)


def _restore_column(eng: Engine, step: Step, field: str) -> None:
    """从备份表分块恢复某列。"""
    bak = eng.backup_table(step)

    def fetch(cursor, limit):
        return eng.conn.execute(
            f"SELECT rowid AS rid, value AS v FROM {_q(bak)} WHERE rowid > ? "
            f"ORDER BY rowid LIMIT ?", (cursor, limit)).fetchall()

    def apply(rows):
        eng.conn.executemany(
            f"UPDATE {_q(eng.migration.table)} SET {_q(field)}=? WHERE rowid=?",
            [(r["v"], r["rid"]) for r in rows])

    eng.run_chunked(step, "rollback", f"restore_{field}", fetch, apply)


def _drop_column(eng: Engine, field: str) -> None:
    if eng.has_column(field):
        eng.conn.execute(
            f"ALTER TABLE {_q(eng.migration.table)} DROP COLUMN {_q(field)}")
        eng.conn.commit()


def _add_column(eng: Engine, field: str, field_type: str,
                not_null: bool = False, default=None) -> None:
    if eng.has_column(field):
        return
    sql = f"ALTER TABLE {_q(eng.migration.table)} ADD COLUMN {_q(field)} {field_type}"
    if default is not None:
        lit = default if isinstance(default, (int, float)) else "'" + str(default).replace("'", "''") + "'"
        sql += f" DEFAULT {lit}"
    if not_null:
        sql += " NOT NULL" + ("" if default is not None else " DEFAULT 0")
    eng.conn.execute(sql)
    eng.conn.commit()


# ---------- add_field ----------

def _add_field_forward(eng: Engine, step: Step) -> None:
    p = step.params
    _add_column(eng, p["field"], p["field_type"],
                not_null=bool(p.get("not_null")), default=p.get("default"))
    if "default" in p and p.get("backfill", True):
        _backfill(eng, step, p["field"], p["default"], "forward")


def _add_field_rollback(eng: Engine, step: Step) -> None:
    _drop_column(eng, step.params["field"])


# ---------- drop_field ----------

def _drop_field_forward(eng: Engine, step: Step) -> None:
    field = step.params["field"]
    if eng.has_column(field):
        eng.save_backup_meta(step, {"field": field,
                                    "field_type": eng.columns().get(field, "TEXT")})
        _backup_column(eng, step, field)
        _drop_column(eng, field)


def _drop_field_rollback(eng: Engine, step: Step) -> None:
    meta = eng.load_backup_meta(step)
    if not meta:
        return
    field = meta["field"]
    _add_column(eng, field, meta.get("field_type", "TEXT"))
    _restore_column(eng, step, field)


# ---------- backfill ----------

def _backfill(eng: Engine, step: Step, field: str, value, direction: str,
              match=None) -> None:
    t = _q(eng.migration.table)
    cond = f"{_q(field)} IS NULL" if match is None else f"{_q(field)} = ?"

    def fetch(cursor, limit):
        args = (cursor, limit) if match is None else (match, cursor, limit)
        return eng.conn.execute(
            f"SELECT rowid AS rid FROM {t} WHERE {cond} AND rowid > ? "
            f"ORDER BY rowid LIMIT ?", args).fetchall()

    def apply(rows):
        eng.conn.executemany(
            f"UPDATE {t} SET {_q(field)}=? WHERE rowid=?",
            [(value, r["rid"]) for r in rows])

    eng.run_chunked(step, direction, f"backfill_{field}", fetch, apply)


def _backfill_forward(eng: Engine, step: Step) -> None:
    p = step.params
    match = p.get("match")  # 默认只回填 NULL 行；指定 match 则回填等于该值的行
    # 先备份受影响行的原值，保证回滚精确还原。
    bak = _ensure_backup_table(eng, step)
    t = _q(eng.migration.table)
    cond = f"{_q(p['field'])} IS NULL" if match is None else f"{_q(p['field'])} = ?"

    def fetch_old(cursor, limit):
        args = (cursor, limit) if match is None else (match, cursor, limit)
        return eng.conn.execute(
            f"SELECT rowid AS rid, {_q(p['field'])} AS v FROM {t} "
            f"WHERE {cond} AND rowid > ? "
            f"AND rowid NOT IN (SELECT rowid FROM {_q(bak)}) "
            f"ORDER BY rowid LIMIT ?", args).fetchall()

    def save_old(rows):
        eng.conn.executemany(
            f"INSERT OR IGNORE INTO {_q(bak)}(rowid, value) VALUES(?, ?)",
            [(r["rid"], r["v"]) for r in rows])

    eng.run_chunked(step, "forward", f"backup_old_{p['field']}", fetch_old, save_old)
    _backfill(eng, step, p["field"], p["value"], "forward", match=match)


def _backfill_rollback(eng: Engine, step: Step) -> None:
    # 只把前向阶段备份过的行恢复为 NULL，不误伤迁移后新写入的同名值。
    _restore_column(eng, step, step.params["field"])


# ---------- split_field ----------

def _split_forward(eng: Engine, step: Step) -> None:
    p = step.params
    targets = p["targets"]
    ttype = p.get("target_type", "TEXT")
    for tgt in targets:
        _add_column(eng, tgt, ttype)
    src, delim = p["source"], p.get("delimiter", " ")
    t = _q(eng.migration.table)

    def fetch(cursor, limit):
        return eng.conn.execute(
            f"SELECT rowid AS rid, {_q(src)} AS v FROM {t} WHERE rowid > ? "
            f"ORDER BY rowid LIMIT ?", (cursor, limit)).fetchall()

    def apply(rows):
        sets = ", ".join(f"{_q(x)}=?" for x in targets)
        eng.conn.executemany(
            f"UPDATE {t} SET {sets} WHERE rowid=?",
            [(*split_value(r["v"], delim, len(targets)), r["rid"]) for r in rows])

    eng.run_chunked(step, "forward", "split", fetch, apply)
    if not p.get("keep_source", True):
        eng.save_backup_meta(step, {"field": src,
                                    "field_type": eng.columns().get(src, "TEXT")})
        _backup_column(eng, step, src)
        _drop_column(eng, src)


def _split_rollback(eng: Engine, step: Step) -> None:
    p = step.params
    if not p.get("keep_source", True):
        meta = eng.load_backup_meta(step)
        if meta:
            _add_column(eng, meta["field"], meta.get("field_type", "TEXT"))
            _restore_column(eng, step, meta["field"])
    for tgt in p["targets"]:
        _drop_column(eng, tgt)


# ---------- merge_fields ----------

def _merge_forward(eng: Engine, step: Step) -> None:
    p = step.params
    sources, target = p["sources"], p["target"]
    sep = p.get("separator", " ")
    _add_column(eng, target, p.get("target_type", "TEXT"))
    t = _q(eng.migration.table)
    cols = ", ".join(_q(s) for s in sources)

    def fetch(cursor, limit):
        return eng.conn.execute(
            f"SELECT rowid AS rid, {cols} FROM {t} WHERE rowid > ? "
            f"ORDER BY rowid LIMIT ?", (cursor, limit)).fetchall()

    def apply(rows):
        eng.conn.executemany(
            f"UPDATE {t} SET {_q(target)}=? WHERE rowid=?",
            [(sep.join("" if r[s] is None else str(r[s]) for s in sources), r["rid"])
             for r in rows])

    eng.run_chunked(step, "forward", "merge", fetch, apply)
    if not p.get("keep_sources", True):
        for src in sources:
            bak_step = Step(id=f"{step.id}__{src}", type="drop_field",
                            params={"field": src})
            eng.save_backup_meta(bak_step, {"field": src,
                                            "field_type": eng.columns().get(src, "TEXT")})
            _backup_column(eng, bak_step, src)
            _drop_column(eng, src)


def _merge_rollback(eng: Engine, step: Step) -> None:
    p = step.params
    if not p.get("keep_sources", True):
        for src in p["sources"]:
            bak_step = Step(id=f"{step.id}__{src}", type="drop_field",
                            params={"field": src})
            meta = eng.load_backup_meta(bak_step)
            if meta:
                _add_column(eng, src, meta.get("field_type", "TEXT"))
                _restore_column(eng, bak_step, src)
    _drop_column(eng, p["target"])


# ---------- change_type ----------

def _change_type_forward(eng: Engine, step: Step) -> None:
    p = step.params
    field, tmp = p["field"], f"__new_{p['field']}"
    conv = p.get("conversion", {"kind": "cast", "to": p["to"]})
    t = _q(eng.migration.table)
    if eng.has_column(field):
        eng.save_backup_meta(step, {"field": field,
                                    "field_type": eng.columns().get(field, "TEXT")})
        _backup_column(eng, step, field)
    _add_column(eng, tmp, p["to"])

    def fetch(cursor, limit):
        return eng.conn.execute(
            f"SELECT rowid AS rid, {_q(field)} AS v FROM {t} WHERE rowid > ? "
            f"ORDER BY rowid LIMIT ?", (cursor, limit)).fetchall()

    def apply(rows):
        eng.conn.executemany(
            f"UPDATE {t} SET {_q(tmp)}=? WHERE rowid=?",
            [(convert_value(r["v"], conv), r["rid"]) for r in rows])

    if eng.has_column(field):
        eng.run_chunked(step, "forward", "convert", fetch, apply)
        _drop_column(eng, field)
    if eng.has_column(tmp):
        eng.conn.execute(
            f"ALTER TABLE {t} RENAME COLUMN {_q(tmp)} TO {_q(field)}")
        eng.conn.commit()


def _change_type_rollback(eng: Engine, step: Step) -> None:
    meta = eng.load_backup_meta(step)
    if not meta:
        return
    field, tmp = meta["field"], f"__old_{meta['field']}"
    t = _q(eng.migration.table)
    _add_column(eng, tmp, meta.get("field_type", "TEXT"))
    bak = eng.backup_table(step)

    def fetch(cursor, limit):
        return eng.conn.execute(
            f"SELECT rowid AS rid, value AS v FROM {_q(bak)} WHERE rowid > ? "
            f"ORDER BY rowid LIMIT ?", (cursor, limit)).fetchall()

    def apply(rows):
        eng.conn.executemany(
            f"UPDATE {t} SET {_q(tmp)}=? WHERE rowid=?",
            [(r["v"], r["rid"]) for r in rows])

    eng.run_chunked(step, "rollback", "restore_type", fetch, apply)
    _drop_column(eng, field)
    if eng.has_column(tmp):
        eng.conn.execute(
            f"ALTER TABLE {t} RENAME COLUMN {_q(tmp)} TO {_q(field)}")
        eng.conn.commit()


# ---------- rebuild_index ----------

def _rebuild_index_forward(eng: Engine, step: Step) -> None:
    p = step.params
    t = _q(eng.migration.table)
    eng.conn.execute(f"DROP INDEX IF EXISTS {_q(p['index'])}")
    cols = ", ".join(_q(f) for f in p["fields"])
    unique = "UNIQUE " if p.get("unique") else ""
    eng.conn.execute(
        f"CREATE {unique}INDEX IF NOT EXISTS {_q(p['index'])} ON {t} ({cols})")
    eng.conn.commit()


def _rebuild_index_rollback(eng: Engine, step: Step) -> None:
    # 索引不影响数据语义；回滚即删除新索引。
    eng.conn.execute(f"DROP INDEX IF EXISTS {_q(step.params['index'])}")
    eng.conn.commit()


FORWARD = {
    "add_field": _add_field_forward,
    "drop_field": _drop_field_forward,
    "split_field": _split_forward,
    "merge_fields": _merge_forward,
    "change_type": _change_type_forward,
    "backfill_default": _backfill_forward,
    "rebuild_index": _rebuild_index_forward,
}

ROLLBACK = {
    "add_field": _add_field_rollback,
    "drop_field": _drop_field_rollback,
    "split_field": _split_rollback,
    "merge_fields": _merge_rollback,
    "change_type": _change_type_rollback,
    "backfill_default": _backfill_rollback,
    "rebuild_index": _rebuild_index_rollback,
}


def forward_step(eng: Engine, step: Step) -> None:
    FORWARD[step.type](eng, step)


def rollback_step(eng: Engine, step: Step) -> None:
    ROLLBACK[step.type](eng, step)
