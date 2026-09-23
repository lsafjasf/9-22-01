"""迁移描述（spec）的加载、校验与依赖排序。

迁移描述是一个 JSON 文件，示例见 examples/migration.json::

    {
      "name": "user-store",
      "from_version": 1,
      "to_version": 2,
      "steps": [
        {"id": "add_status", "type": "add_field", "field": "status",
         "default": "active"},
        {"id": "rebuild_idx", "type": "rebuild_index", "index": "by_status",
         "field": "status", "depends_on": ["add_status"]}
      ]
    }
"""

import json
from dataclasses import dataclass, field


class SpecError(Exception):
    """迁移描述不合法。"""


@dataclass
class Step:
    id: str
    type: str
    params: dict
    depends_on: list = field(default_factory=list)

    def get(self, key, default=None):
        return self.params.get(key, default)


@dataclass
class Spec:
    name: str
    from_version: int
    to_version: int
    steps: list  # 已按依赖拓扑排序


def load_spec(path) -> Spec:
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    return parse_spec(raw)


def parse_spec(raw: dict) -> Spec:
    from .steps import HANDLERS  # 延迟导入，避免循环依赖

    if not isinstance(raw, dict):
        raise SpecError("迁移描述必须是 JSON 对象")
    name = raw.get("name", "unnamed")
    from_version = raw.get("from_version")
    to_version = raw.get("to_version")
    if from_version is None or to_version is None:
        raise SpecError("缺少 from_version / to_version")
    raw_steps = raw.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise SpecError("steps 必须是非空数组")

    steps = []
    seen = set()
    for i, item in enumerate(raw_steps):
        if not isinstance(item, dict):
            raise SpecError(f"steps[{i}] 必须是对象")
        sid = item.get("id")
        stype = item.get("type")
        if not sid:
            raise SpecError(f"steps[{i}] 缺少 id")
        if sid in seen:
            raise SpecError(f"步骤 id 重复: {sid}")
        seen.add(sid)
        handler = HANDLERS.get(stype)
        if handler is None:
            raise SpecError(f"步骤 {sid}: 未知类型 {stype!r}，"
                            f"支持: {sorted(HANDLERS)}")
        params = {k: v for k, v in item.items()
                  if k not in ("id", "type", "depends_on")}
        for key in handler.required:
            if key not in params:
                raise SpecError(f"步骤 {sid} ({stype}) 缺少参数: {key}")
        depends_on = item.get("depends_on", [])
        if isinstance(depends_on, str):
            depends_on = [depends_on]
        steps.append(Step(id=sid, type=stype, params=params,
                          depends_on=list(depends_on)))

    return Spec(name=name, from_version=from_version, to_version=to_version,
                steps=_topo_sort(steps))


def _topo_sort(steps):
    """稳定的 Kahn 拓扑排序：同层保持声明顺序。"""
    by_id = {s.id: s for s in steps}
    for s in steps:
        for dep in s.depends_on:
            if dep not in by_id:
                raise SpecError(f"步骤 {s.id} 依赖不存在的步骤: {dep}")
            if dep == s.id:
                raise SpecError(f"步骤 {s.id} 不能依赖自身")

    indegree = {s.id: 0 for s in steps}
    children = {s.id: [] for s in steps}
    for s in steps:
        for dep in s.depends_on:
            indegree[s.id] += 1
            children[dep].append(s.id)

    ready = [s.id for s in steps if indegree[s.id] == 0]
    ordered = []
    while ready:
        sid = ready.pop(0)
        ordered.append(by_id[sid])
        for child in children[sid]:
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
    if len(ordered) != len(steps):
        remaining = [sid for sid, d in indegree.items() if d > 0]
        raise SpecError(f"步骤依赖存在环，涉及: {remaining}")
    return ordered
