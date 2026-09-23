"""声明式迁移描述：解析、校验、依赖拓扑排序。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

STEP_TYPES = {
    "add_field",
    "drop_field",
    "split_field",
    "merge_fields",
    "change_type",
    "backfill_default",
    "rebuild_index",
}


class MigrationError(Exception):
    pass


@dataclass
class Step:
    id: str
    type: str
    params: dict[str, Any]
    depends_on: list[str] = field(default_factory=list)


@dataclass
class Migration:
    migration_id: str
    table: str
    batch_size: int
    steps: list[Step]
    description: str = ""

    def ordered_steps(self) -> list[Step]:
        return topo_sort(self.steps)


def topo_sort(steps: list[Step]) -> list[Step]:
    by_id: dict[str, Step] = {}
    for s in steps:
        if s.id in by_id:
            raise MigrationError(f"重复步骤 id: {s.id}")
        by_id[s.id] = s
    for s in steps:
        for dep in s.depends_on:
            if dep not in by_id:
                raise MigrationError(f"步骤 {s.id} 依赖不存在的步骤 {dep}")

    ordered: list[Step] = []
    state: dict[str, int] = {}  # 0=未访问 1=访问中 2=完成

    def visit(s: Step, stack: list[str]) -> None:
        st = state.get(s.id, 0)
        if st == 2:
            return
        if st == 1:
            raise MigrationError("依赖存在环: " + " -> ".join(stack + [s.id]))
        state[s.id] = 1
        for dep in s.depends_on:
            visit(by_id[dep], stack + [s.id])
        state[s.id] = 2
        ordered.append(s)

    for s in steps:
        visit(s, [])
    return ordered


def _require(step_id: str, params: dict, keys: list[str]) -> None:
    for k in keys:
        if k not in params:
            raise MigrationError(f"步骤 {step_id} 缺少必填参数 {k}")


def validate_step(s: Step) -> None:
    if s.type not in STEP_TYPES:
        raise MigrationError(f"步骤 {s.id} 类型未知: {s.type}")
    p = s.params
    if s.type == "add_field":
        _require(s.id, p, ["field", "field_type"])
    elif s.type == "drop_field":
        _require(s.id, p, ["field"])
    elif s.type == "split_field":
        _require(s.id, p, ["source", "targets"])
        if len(p["targets"]) < 2:
            raise MigrationError(f"步骤 {s.id} split_field 至少需要两个目标字段")
    elif s.type == "merge_fields":
        _require(s.id, p, ["sources", "target"])
        if len(p["sources"]) < 2:
            raise MigrationError(f"步骤 {s.id} merge_fields 至少需要两个源字段")
    elif s.type == "change_type":
        _require(s.id, p, ["field", "to"])
    elif s.type == "backfill_default":
        _require(s.id, p, ["field", "value"])
    elif s.type == "rebuild_index":
        _require(s.id, p, ["index", "fields"])


def load_migration(path: str) -> Migration:
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    steps = []
    for s in raw.get("steps", []):
        params = {k: v for k, v in s.items() if k not in ("id", "type", "depends_on")}
        step = Step(
            id=s["id"],
            type=s["type"],
            params=params,
            depends_on=list(s.get("depends_on", [])),
        )
        validate_step(step)
        steps.append(step)
    if not steps:
        raise MigrationError("迁移描述不包含任何步骤")
    mig = Migration(
        migration_id=raw["migration_id"],
        table=raw["table"],
        batch_size=int(raw.get("batch_size", 500)),
        steps=steps,
        description=raw.get("description", ""),
    )
    mig.ordered_steps()  # 提前校验依赖
    return mig
