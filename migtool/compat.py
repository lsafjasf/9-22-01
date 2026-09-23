"""执行前兼容性评估：判断每个步骤对旧版本读写的影响。"""
from __future__ import annotations

from dataclasses import dataclass

from .model import Migration, Step

SAFE = "SAFE"
RISKY = "RISKY"
BREAKING = "BREAKING"

_WIDENING = {("INTEGER", "REAL"), ("INTEGER", "TEXT"), ("REAL", "TEXT"), ("INTEGER", "NUMERIC")}


@dataclass
class Finding:
    step_id: str
    level: str
    reason: str


def evaluate_step(step: Step) -> Finding:
    p = step.params
    t = step.type
    if t == "add_field":
        if p.get("not_null") and "default" not in p:
            return Finding(step.id, BREAKING,
                           f"新增 NOT NULL 字段 {p['field']} 且无默认值：旧版本写入（INSERT 不带该列）会失败")
        return Finding(step.id, SAFE,
                       f"新增字段 {p['field']}：旧版本按列名读写，未知列被忽略；"
                       "若旧代码使用 SELECT * 并做严格列映射则需确认")
    if t == "drop_field":
        return Finding(step.id, BREAKING,
                       f"删除字段 {p['field']}：旧版本读/写该列的 SQL 会直接报错，"
                       "必须确认所有旧版本实例已不再引用该字段")
    if t == "split_field":
        if p.get("keep_source", True):
            return Finding(step.id, RISKY,
                           f"拆分 {p['source']} 但保留源列：旧版本可读源列不中断，"
                           f"但旧版本只写源列，新列 {p['targets']} 在双写切换前会过期（读新列可能拿到旧值）")
        return Finding(step.id, BREAKING,
                       f"拆分 {p['source']} 并删除源列：旧版本读写 {p['source']} 会失败")
    if t == "merge_fields":
        if p.get("keep_sources", True):
            return Finding(step.id, RISKY,
                           f"合并 {p['sources']} 为 {p['target']} 但保留源列：旧版本读写源列不中断，"
                           "但旧版本写入不会同步到合并列，切换前合并列会过期")
        return Finding(step.id, BREAKING,
                       f"合并并删除源列 {p['sources']}：旧版本读写源列会失败")
    if t == "change_type":
        frm = str(p.get("from", "")).upper()
        to = str(p["to"]).upper()
        if (frm, to) in _WIDENING:
            return Finding(step.id, RISKY,
                           f"字段 {p['field']} 类型 {frm} -> {to} 为放宽转换：旧版本写入一般兼容，"
                           "但旧版本可能读到超出其预期的格式")
        return Finding(step.id, BREAKING,
                       f"字段 {p['field']} 类型 {frm} -> {to} 为收窄/有损转换："
                       "旧版本可能写入无法转换的值，或读回被截断/转换后的值")
    if t == "backfill_default":
        return Finding(step.id, SAFE,
                       f"回填 {p['field']} 默认值：纯数据操作，旧版本可继续读写；"
                       "注意旧读者会看到回填后的值而非 NULL")
    if t == "rebuild_index":
        return Finding(step.id, SAFE,
                       f"重建索引 {p['index']}：仅影响查询性能，不影响读写语义；"
                       "建索引期间查询可能短暂变慢")
    return Finding(step.id, RISKY, f"未知步骤类型 {t}，按有风险处理")


def evaluate(migration: Migration) -> list[Finding]:
    return [evaluate_step(s) for s in migration.ordered_steps()]


def has_breaking(findings: list[Finding]) -> bool:
    return any(f.level == BREAKING for f in findings)


def format_report(findings: list[Finding]) -> str:
    lines = []
    for f in findings:
        mark = {SAFE: "[安全]", RISKY: "[风险]", BREAKING: "[破坏]"}[f.level]
        lines.append(f"  {mark} {f.step_id}: {f.reason}")
    return "\n".join(lines)
