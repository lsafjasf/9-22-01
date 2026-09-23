# migtool — 声明式数据迁移工具

纯 Python 3 标准库实现（仅依赖 `sqlite3`/`json`/`argparse` 等），面向"已上线版本与待上线版本同时读写同一份数据"的长期演进存储系统。数据存储以 SQLite 库中的表为模型：字段=列，索引=SQLite 索引。

## 能力总览

- **声明式步骤**：`add_field` / `drop_field` / `split_field` / `merge_fields` / `change_type` / `backfill_default` / `rebuild_index`，步骤间用 `depends_on` 声明依赖，执行前做拓扑排序并检测环。
- **执行前兼容性评估**：`check` 命令输出每个步骤对旧版本读写的影响（SAFE / RISKY / BREAKING）及理由；存在 BREAKING 步骤时，`apply` 必须显式加 `--confirm-breaking` 才执行。
- **可中断、可重入**：所有数据变更按 `batch_size` 分块，每批提交一次并推进 `_mig_journal` 中的游标；进程被强杀后重启，从游标继续，已提交批次不会重放；所有 DDL 先做存在性检查，天然幂等。
- **回滚与前向修复**：`rollback` 按逆拓扑序执行各步骤的逆操作；`apply --strategy forward-fix` 以补偿式迁移修复问题（审计中标记策略）。
- **审计**：全程写入 `_mig_audit` 表（时间戳、事件、步骤、参数、耗时），`audit` 命令查看。
- **副本预演**：`dry-run` 用 SQLite backup API 复制出库副本（或抽样 N 行），完整执行迁移并统计每步变更行数、实测耗时、按行数比外推的预估耗时，并可选在副本上验证回滚保真度。

## 运行命令

```bash
# 生成演示库（users 表，1 万行）
python3 -m migtool init-demo --db demo.db --rows 10000

# 1. 执行前兼容性评估（不执行任何变更；有破坏性步骤时退出码为 1）
python3 -m migtool check --db demo.db --migration examples/migration.json

# 2. 副本预演：全量副本；--sample-rows N 可抽样并按行数比外推耗时
python3 -m migtool dry-run --db demo.db --migration examples/migration.json \
    --sample-rows 2000 --report examples/dry_run_report.json

# 3. 执行迁移（存在破坏性步骤时必须显式确认；可随时 Ctrl-C/强杀，重跑即续跑）
python3 -m migtool apply --db demo.db --migration examples/migration.json --confirm-breaking

# 前向修复策略（补偿式迁移，示例见 examples/forward_fix.json）
python3 -m migtool apply --db demo.db --migration examples/forward_fix.json --strategy forward-fix

# 4. 回滚（可中断续跑；重复执行是空操作）
python3 -m migtool rollback --db demo.db --migration examples/migration.json

# 查看执行状态与审计记录
python3 -m migtool status --db demo.db --migration examples/migration.json
python3 -m migtool audit  --db demo.db --migration examples/migration.json

# 运行自测（14 个用例：依赖排序/兼容评估/执行/强杀重入/回滚保真/预演）
python3 -m unittest discover -s tests -v
```

## 迁移描述格式

见 `examples/migration.json`。顶层字段：`migration_id`、`table`、`batch_size`、`steps[]`；每个步骤含 `id`、`type`、`depends_on` 及类型特定参数：

| 类型 | 关键参数 | 说明 |
|---|---|---|
| `add_field` | `field`, `field_type`, `default?`, `not_null?`, `backfill?` | 加列并可选分块回填默认值 |
| `drop_field` | `field` | 先分块备份该列到 `_migbak_<step>`，再删列 |
| `split_field` | `source`, `targets[]`, `delimiter`, `keep_source` | 拆分字符串列到多个新列 |
| `merge_fields` | `sources[]`, `target`, `separator`, `keep_sources` | 合并多列为新列 |
| `change_type` | `field`, `from`, `to`, `conversion` | 备份原值→新列分块转换→删旧列→改名 |
| `backfill_default` | `field`, `value`, `match?` | 回填 NULL 行（或等于 `match` 的行），先备份原值 |
| `rebuild_index` | `index`, `fields[]`, `unique?` | 删旧索引并重建 |

`conversion` 是受限声明式转换器（不使用 `eval`）：`{"kind":"cast","to":"INTEGER|REAL|TEXT|NUMERIC","on_error":"null"}`。

## 兼容性评估规则

- `add_field`：带默认值 → SAFE；`not_null` 且无默认值 → BREAKING（旧版本 INSERT 会失败）。
- `drop_field` / 不保留源列的 `split_field`、`merge_fields` → BREAKING（旧版本读写该列直接报错）。
- 保留源列的拆分/合并 → RISKY（旧版本只写源列，新列在双写切换前会过期）。
- `change_type`：放宽转换（如 INTEGER→TEXT）→ RISKY；收窄/有损转换 → BREAKING。
- `backfill_default` / `rebuild_index` → SAFE（附注意事项）。

## 可中断与可重入的实现

- 元表 `_mig_journal(migration_id, step_id, direction, phase, status, cursor)` 记录每个步骤每个阶段的游标；每批处理完即提交并推进游标。崩溃恢复后从未完成阶段的游标继续，已提交批次不重放。
- 多阶段步骤（如 `change_type`：备份→加临时列→转换→删旧列→改名）的每个 DDL 都有存在性检查，任意点被杀后重跑都能从正确的阶段继续。
- 测试 `test_kill_at_every_point` 在迁移的不同批次点注入强杀，验证每次续跑后终态完全一致。

## 回滚 vs 前向修复：数据损失边界

**回滚（rollback）**——逆向恢复迁移前语义：

- `drop_field`/`change_type`/`backfill_default` 在前向执行前把原值分块备份到 `_migbak_<step>` 表，回滚时精确还原，因此**迁移前已存在的数据零损失**（自测以全表指纹校验"回滚后 == 迁移前"）。
- 损失边界：**迁移完成后、回滚之前**新写入的数据中——(a) 写入新增列（如 `email`、`first_name`）的值随列删除而丢失；(b) 对将被还原列（如被删的 `legacy_flag`）的修改无法保留，因为该列已不存在、无处可写；(c) 回滚只还原前向阶段备份过的行，迁移后新插入的行不受影响。
- 备份表在回滚后保留，由运维确认无误后手动清理（`DROP TABLE _migbak_*`），避免误清理导致无法再次回滚。

**前向修复（forward-fix）**——不回退，直接施加补偿式迁移：

- 不丢失任何已迁移数据，也不需要旧版本配合回退；适合线上已产生大量新数据、回滚代价不可接受的场景。
- 损失边界：无法恢复"迁移前的语义状态"，只能定义并迁移到新的目标状态；若问题数据已被错误转换且未备份，原始值不可恢复（因此破坏性步骤一律先备份再变更）。

## 预演报告

`examples/dry_run_report.json` 为真实生成的样例，包含：每步 `affected_rows`（变更量）、`measured_ms`（副本实测）、`estimated_full_ms`（按生产行数/副本行数外推）、总计预估、`rollback_fidelity`（副本上回滚后与迁移前的指纹比对，`exact` 表示语义完全一致）以及兼容性评估全文。

## 目录结构

```
migtool/            # 库源码
  model.py          # 迁移描述解析、校验、拓扑排序
  compat.py         # 兼容性评估规则
  engine.py         # 执行引擎：journal/audit/分块/续跑
  steps.py          # 七类步骤的前向与回滚实现
  dryrun.py         # 副本预演与报告
  cli.py            # 命令行入口
examples/
  migration.json    # 迁移描述样例（覆盖全部七类步骤与依赖）
  forward_fix.json  # 前向修复策略样例
  dry_run_report.json  # 预演报告样例（真实生成）
tests/test_migtool.py  # 自测（含回滚测试与强杀重入测试）
```
