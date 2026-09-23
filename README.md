# migtool — 声明式数据迁移工具

仅依赖 Python 3 标准库。面向「已上线版本与待上线版本同时读写同一份数据」
的场景：迁移必须**可评估、可中断、可回滚**。

## 目录结构

```
migtool/            库 + CLI（python -m migtool）
  store.py          JSON 文档存储，所有写入均为 临时文件+fsync+os.replace 原子写
  spec.py           迁移描述加载、校验、depends_on 拓扑排序（含环检测）
  steps.py          7 种步骤处理器 + 兼容性规则（safe/warning/breaking）
  engine.py         执行引擎：检查点、快照、审计、回滚、前向修复、副本预演
  cli.py            命令行入口
examples/
  migration.json    迁移描述样例（用户库 v1 -> v2）
  dryrun-report.json 预演报告样例（真实运行生成）
tests/
  test_migtool.py   18 个自测，含强杀重入与回滚测试
```

## 声明式迁移描述

JSON 描述，步骤间用 `depends_on` 声明依赖，执行前做拓扑排序：

```json
{"id": "split_name", "type": "split_field", "field": "name",
 "into": ["first_name", "last_name"], "separator": " ",
 "keep_source": false, "depends_on": ["age_to_int"]}
```

支持的步骤类型：

| 类型 | 说明 | 兼容性级别 |
|---|---|---|
| `add_field` | 新增字段并赋默认值 | warning（旧版本整文档覆盖写会丢新字段） |
| `drop_field` | 删除字段 | breaking |
| `split_field` | 拆分字段（`keep_source` 可选保留源字段） | breaking / warning |
| `merge_fields` | 合并字段（`keep_sources` 可选保留源字段） | breaking / warning |
| `change_type` | 改类型（str/int/float/bool） | breaking |
| `backfill_default` | 只回填缺失/null 的字段 | safe |
| `rebuild_index` | 重建索引（临时文件+原子替换） | safe |

## 兼容性评估

`check` / `run` / `dry-run` 都会先评估：每个步骤标注 safe / warning /
breaking 并给出理由。存在 breaking 步骤时 `run` 直接拒绝执行（退出码 2），
必须显式加 `--yes` 确认。

## 可中断与可重入

- 每个步骤的 `apply_record` 幂等：已变换的记录重放时无任何副作用。
- 每条记录**先写快照（fsync）再改记录**；每批提交后落盘检查点
  `.migration/state.json`。
- 任意时刻强杀（包括「快照已写、记录未写」的窗口）重启后都能从检查点
  继续，不重复已完成的副作用。测试
  `test_kill_mid_step_then_resume_no_duplicate_effects` 与
  `test_kill_between_snapshot_and_write_is_consistent` 覆盖了这两个崩溃点。

## 回滚与前向修复

**向后回滚**（`rollback`）：每个步骤执行前把被触碰字段的原始值写入
`.migration/snapshots/<step>.jsonl`；回滚 = 逆序「删除产出字段 + 写回快照
原值」。因此对步骤触碰的字段，回滚后数据语义与迁移前**逐字节一致**
（`test_rollback_all_restores_exact_semantics` 验证）。

- 数据损失边界：迁移完成后**新版本对产出字段的后续修改**会丢失
  （如迁移后用户改了 `first_name`，回滚后恢复为拆分前的 `name`）；
  步骤未触碰的字段不受影响；`keep_source/keep_sources=true` 时源字段
  不被快照，旧版本对源字段的后续写入完整保留。

**前向修复**（`repair`）：迁移中断/失败后不回退，而是幂等地把剩余步骤
推进到完成。

- 数据损失边界：无额外损失（只完成迁移本身声明的变更），但代价是
  **放弃了回到迁移前语义的可能**——修复完成后旧版本必须同步升级，
  否则会继续读写已变更的结构。

选择建议：旧版本仍在服役且未兼容新结构 → 回滚；旧版本已下线或已兼容、
只是迁移没跑完 → 前向修复。

## 审计与预演

- 全程审计：`.migration/audit.jsonl`（append-only），记录确认拒绝、
  迁移开始/结束、步骤开始/完成、批次提交、回滚等事件。
- `dry-run` 把数据复制到临时副本完整执行，统计每步变更量与耗时；
  `--sample N` 抽样时按数据量线性外推全量预估。原库零改动。

## 运行命令

```bash
# 自测（18 个用例，含强杀重入与回滚测试）
python3 -m unittest discover -s tests -v

# 生成演示库（200 条记录）
python3 -m migtool init-demo /tmp/demo-db --records 200

# 兼容性评估（有破坏性步骤时退出码 2）
python3 -m migtool check /tmp/demo-db examples/migration.json

# 副本预演，输出变更量与预估耗时，并保存 JSON 报告
python3 -m migtool dry-run /tmp/demo-db examples/migration.json \
    --report examples/dryrun-report.json

# 执行迁移（破坏性步骤需 --yes 显式确认，否则拒绝执行）
python3 -m migtool run /tmp/demo-db examples/migration.json --yes

# 中断后前向修复
python3 -m migtool repair /tmp/demo-db examples/migration.json

# 回滚：--all 全部回滚 / --to-step <id> 回到指定步骤 / 默认回滚最后一步
python3 -m migtool rollback /tmp/demo-db examples/migration.json --all

# 查看状态与审计日志
python3 -m migtool status /tmp/demo-db
python3 -m migtool audit /tmp/demo-db
```
