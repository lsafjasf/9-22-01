"""迁移步骤处理器。

每种步骤类型实现一个 Handler：

- ``compat(step)``        兼容性评估，返回 [(级别, 理由), ...]，
                          级别为 safe / warning / breaking。
- ``snapshot_fields``     执行前需要快照的字段（用于精确回滚）。
- ``output_fields``       步骤产出的字段（回滚时删除）。
- ``apply_record``        对单条记录的就地变换，**必须幂等**：
                          已变换的记录再次执行时不产生任何副作用。

回滚的通用语义（engine 实现）：删除 output_fields + snapshot_fields，
再把快照中的原始值写回。因此只要快照完整，回滚后数据语义与迁移前一致。
"""

import copy

SAFE = "safe"
WARNING = "warning"
BREAKING = "breaking"

_TYPE_NAMES = {"str": str, "int": int, "float": float, "bool": bool}


class StepError(Exception):
    """步骤执行失败（如类型转换失败）。"""


def _matches_type(value, type_name: str) -> bool:
    if type_name == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "float":
        return isinstance(value, float)
    return isinstance(value, _TYPE_NAMES[type_name])


class Handler:
    type = ""
    required = ()
    record_level = True

    def compat(self, step):
        return []

    def snapshot_fields(self, step):
        return []

    def output_fields(self, step):
        return []

    def apply_record(self, doc, step):
        raise NotImplementedError


class AddField(Handler):
    type = "add_field"
    required = ("field", "default")

    def compat(self, step):
        return [
            (SAFE, "新增字段不影响旧版本读取（旧版本应忽略未知字段）"),
            (WARNING, "若旧版本以整文档覆盖方式写入，会丢弃该新字段；"
                      "需确认旧版本只写字段级更新"),
        ]

    def snapshot_fields(self, step):
        return [step.get("field")]

    def apply_record(self, doc, step):
        field = step.get("field")
        if field in doc:
            return False
        doc[field] = copy.deepcopy(step.get("default"))
        return True


class DropField(Handler):
    type = "drop_field"
    required = ("field",)

    def compat(self, step):
        field = step.get("field")
        return [
            (BREAKING, f"删除字段 {field!r} 后，旧版本读取该字段将得到缺失值，"
                       f"旧版本写入可能重新引入该字段造成脏数据"),
        ]

    def snapshot_fields(self, step):
        return [step.get("field")]

    def apply_record(self, doc, step):
        field = step.get("field")
        if field not in doc:
            return False
        del doc[field]
        return True


class SplitField(Handler):
    type = "split_field"
    required = ("field", "into")

    def compat(self, step):
        field = step.get("field")
        if step.get("keep_source", False):
            return [
                (WARNING, f"保留源字段 {field!r}：旧版本只写源字段，"
                          f"拆分出的新字段在旧版本写入后会暂时过期"),
            ]
        return [
            (BREAKING, f"拆分后删除源字段 {field!r}，"
                       f"旧版本读写该字段将失败"),
        ]

    def snapshot_fields(self, step):
        return [] if step.get("keep_source", False) else [step.get("field")]

    def output_fields(self, step):
        return list(step.get("into"))

    def apply_record(self, doc, step):
        field = step.get("field")
        into = list(step.get("into"))
        keep = step.get("keep_source", False)
        if field not in doc:
            return False  # 已拆分过
        if keep and all(t in doc for t in into):
            return False  # 已拆分过（保留源字段模式）
        value = doc[field]
        if not isinstance(value, str):
            raise StepError(f"split_field: 字段 {field!r} 的值不是字符串: {value!r}")
        sep = step.get("separator", " ")
        parts = value.split(sep, len(into) - 1)
        parts += [""] * (len(into) - len(parts))
        for target, part in zip(into, parts):
            doc[target] = part
        if not keep:
            del doc[field]
        return True


class MergeFields(Handler):
    type = "merge_fields"
    required = ("fields", "into")

    def compat(self, step):
        fields = step.get("fields")
        if step.get("keep_sources", False):
            return [
                (WARNING, f"保留源字段 {fields}：旧版本只写源字段，"
                          f"合并字段在旧版本写入后会暂时过期"),
            ]
        return [
            (BREAKING, f"合并后删除源字段 {fields}，"
                       f"旧版本读写这些字段将失败"),
        ]

    def snapshot_fields(self, step):
        return [] if step.get("keep_sources", False) else list(step.get("fields"))

    def output_fields(self, step):
        return [step.get("into")]

    def apply_record(self, doc, step):
        fields = list(step.get("fields"))
        into = step.get("into")
        keep = step.get("keep_sources", False)
        if not all(f in doc for f in fields):
            return False  # 已合并过（源字段已删）或记录缺源字段
        sep = step.get("separator", " ")
        doc[into] = sep.join(str(doc[f]) for f in fields)
        if not keep:
            for f in fields:
                del doc[f]
        return True


class ChangeType(Handler):
    type = "change_type"
    required = ("field", "from", "to")

    def compat(self, step):
        field, frm, to = step.get("field"), step.get("from"), step.get("to")
        issues = [
            (BREAKING, f"字段 {field!r} 类型 {frm} -> {to}，"
                       f"旧版本按 {frm} 解析将失败"),
        ]
        if (frm, to) in (("float", "int"), ("int", "bool")):
            issues.append((WARNING, "该转换为有损转换，回滚依赖快照恢复原值"))
        return issues

    def snapshot_fields(self, step):
        return [step.get("field")]

    def apply_record(self, doc, step):
        field, frm, to = step.get("field"), step.get("from"), step.get("to")
        if frm not in _TYPE_NAMES or to not in _TYPE_NAMES:
            raise StepError(f"change_type: 不支持的类型 {frm!r}/{to!r}，"
                            f"支持: {sorted(_TYPE_NAMES)}")
        if field not in doc:
            return False
        value = doc[field]
        if _matches_type(value, to) and not _matches_type(value, frm):
            return False  # 已转换过
        if not _matches_type(value, frm):
            return False  # 类型不符，跳过（视为无需转换）
        try:
            doc[field] = _TYPE_NAMES[to](value)
        except (ValueError, TypeError) as exc:
            raise StepError(
                f"change_type: 字段 {field!r} 值 {value!r} 无法转为 {to}") from exc
        return True


class BackfillDefault(Handler):
    type = "backfill_default"
    required = ("field", "default")

    def compat(self, step):
        return [
            (SAFE, "仅回填缺失或为 null 的字段，不覆盖已有值，"
                   "对旧版本读写无影响"),
        ]

    def snapshot_fields(self, step):
        return [step.get("field")]

    def apply_record(self, doc, step):
        field = step.get("field")
        if field in doc and doc[field] is not None:
            return False
        doc[field] = copy.deepcopy(step.get("default"))
        return True


class RebuildIndex(Handler):
    """存储级步骤：不逐条处理记录，整体重建索引文件。"""

    type = "rebuild_index"
    required = ("index", "field")
    record_level = False

    def compat(self, step):
        return [
            (SAFE, f"索引 {step.get('index')!r} 采用临时文件 + 原子替换重建，"
                   f"重建期间旧索引持续可用，不影响数据读写"),
        ]

    def apply_store(self, store, step):
        return store.rebuild_index(step.get("index"), step.get("field"))


HANDLERS = {h.type: h() for h in (
    AddField, DropField, SplitField, MergeFields, ChangeType,
    BackfillDefault, RebuildIndex,
)}


def get_handler(step_type: str) -> Handler:
    return HANDLERS[step_type]
