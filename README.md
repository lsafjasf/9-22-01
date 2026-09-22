# two_phase_commit — 两阶段提交协调者/参与方库（Python 标准库实现）

包含：库、内置故障注入模拟通道、命令行工具（演示 / 自测 / 只读取证）、
完整故障注入测试集。仅使用 Python 3 标准库。

## 目录结构

```
two_phase_commit/
  protocol.py      协议消息（JSON over 模拟通道）
  storage.py       fsync 的仅追加日志（CRC、撕裂/损坏检测）
  sim.py           离散事件模拟器 + 可注入延迟/丢包/重复/乱序/分区的通道
  coordinator.py   协调者状态机与恢复
  participant.py   参与方状态机、阻塞清单、幂等 apply、恢复查询
  forensics.py     只读日志取证（证据 + 建议决议，绝不自动猜测）
  cli.py           demo / inspect / selftest 命令
  tests/           故障注入测试集（27 个用例，含 60 组随机混沌历史）
  docs/
    state_machine.md  状态机与每个状态的持久化时机
    recovery.md       恢复流程与人工取证守则
```

## 运行

所有命令在仓库根目录（本目录）执行：

```bash
# 1) 全量故障注入自测
python3 -m two_phase_commit.cli selftest
# 或直接用 unittest
python3 -m unittest discover -s two_phase_commit/tests -p 'test_*.py' -t .

# 2) 故障场景演示（含决议、各方状态、丢包/重复/乱序统计）
#    场景：happy | vote-no | kill-coordinator | kill-participant |
#          partition | durability-order | chaos
python3 -m two_phase_commit.cli demo chaos --seed 7 --keep-logs --datadir runs/7
python3 -m two_phase_commit.cli demo kill-coordinator --seed 3 --json

# 3) 只读取证/恢复工具（不会修改任何日志）
python3 -m two_phase_commit.cli inspect runs/7
python3 -m two_phase_commit.cli inspect runs/7 --json
# 退出码 0：结论安全；3：歧义/证据不足，必须人工判定
```

## 关键安全性质

- 所有决议先 `fsync` 落盘、后发送；协调者重启严格沿用已持久化决议。
- 崩溃发生在投票阶段 → 推定中止（此时不可能已有 COMMIT 流出）。
- 参与方对重复 COMMIT/ABORT 幂等；未收决议时保持 PREPARED 阻塞，
  可经 `Participant.blocked()` 查询，并通过 `STATUS_QUERY` 自动收敛。
- 日志损坏时拒绝自动猜测：`inspect` 只给出证据、建议与人工步骤。
- 测试在参与方崩溃、协调者强杀、分区、重复/迟到/乱序消息组合下断言：
  不得出现部分提交、部分回滚，结束时阻塞清单必须为空。
