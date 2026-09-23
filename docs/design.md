# 两阶段提交（2PC）设计文档

## 总览

- `tpc/coordinator.py` — 协调者状态机
- `tpc/participant.py` — 参与方状态机
- `tpc/sim.py` — 模拟通道（虚拟时钟 + 延迟/丢包/重复/乱序/分区注入）
- `tpc/logstore.py` — 追加式持久日志（每条记录写后 `fsync`）
- `tpc/doctor.py` — 只读恢复工具（不修改任何文件）
- `tpc/cli.py` — 命令行入口

核心不变式：**任何决议先持久化（fsync）再发送**。因此无论谁在何时被强杀，
重启后都能从日志重建唯一确定的决议，绝不出现部分提交、部分回滚。

## 协调者状态机

```
        begin              全部 yes           任一 no / 投票超时
(无) ---------> PREPARING ---------> COMMITTING ------+
                 |    \                              |
                 |     \____________ 投票超时 _______ |
                 |                                   v
                 |            决议持久化后重发直到全部 ACK
                 |                                   |
                 +---------> ABORTING <--------------+
                              |
                       全部 ACK v
                             DONE
```

### 持久化时机（全部先写日志再发消息）

| 记录 | 时机 | 之后才能做 |
|---|---|---|
| `begin` | 发起事务时 | 发送 PREPARE |
| `vote` | 每收到一票 | 让该票影响决议 |
| `decide` | 达成决议的瞬间 | 发送任何 DECISION 消息 |
| `ack` | 每收到一个 ACK | 计入完成度 |
| `done` | 收齐全部 ACK | 结束事务 |

易失状态（崩溃即丢失，重启重建）：投票超时截止时间、重发计时器。

### 超时与重试

- **投票超时**（`vote_timeout`）：PREPARING 超时 → 决议 ABORT（先持久化再通知）。
- **PREPARE 重发**：对未投票的参与方按 `resend_interval` 重发 PREPARE（参与方幂等）。
- **DECISION 重发**：对未 ACK 的参与方按 `resend_interval` 重发，直到全部 ACK。

### 协调者恢复流程

1. 用 `read_strict()` 重放日志；**发现损坏行即拒绝自动恢复**（宁停不错），
   提示使用 `doctor` 工具人工判定。
2. 对每个未完成事务：
   - 已有 `decide` 记录 → 向未 ACK 的参与方重发该决议。
   - 无 `decide` 记录 → ** presume abort**：决议不可能已发给任何参与方
     （发送前必须先持久化），因此安全地持久化 DECIDE ABORT 并通知。

## 参与方状态机

```
        PREPARE            DECISION commit
(无) ----------> PREPARED ------------> COMMITTED
        (投票先持久化)  |
                       | DECISION abort
                       +--------------> ABORTED
```

### 持久化时机

| 记录 | 时机 | 之后才能做 |
|---|---|---|
| `prepared` | 收到 PREPARE、确定投票后 | 回复 VOTE |
| `decision` | 收到决议后 | 应用决议、回复 ACK |
| `conflict` | 收到与已持久化决议矛盾的命令 | 拒绝执行、不 ACK（告警用） |

### 幂等与阻塞

- 重复 PREPARE → 重发原投票；重复 COMMIT/ABORT → 不重复持久化、不重复应用，
  只重发 ACK。
- PREPARED 是**阻塞状态**：未收到决议前事务一直挂起，
  `Participant.blocked()` 返回阻塞清单 `{txn: vote}`；
  CLI `python -m tpc blocked --dir D` 可查询。
- 阻塞中的参与方按 `query_interval` 主动向协调者发 QUERY 询问决议，
  协调者回复 STATUS；这为协调者重发之外的第二条恢复路径。
- 参与方崩溃重启后从日志重放，PREPARED/已决议状态全部保留。

## 一致性论证（为何不会部分提交部分回滚）

1. 协调者只有在持久化 `decide` 之后才会发送 DECISION；
2. 参与方只有在持久化 `decision` 之后才会应用并 ACK；
3. 协调者恢复时：有 `decide` 记录 → 继续推进同一决议；无记录 → presume abort，
   而此时不可能有任何参与方已提交（否则其 `decision` 记录与协调者发送行为矛盾）；
4. 因此决议的唯一来源是协调者日志中的 `decide` 记录，全网最多一个决议值。

## 只读恢复工具（doctor）

当协调者日志损坏、自动恢复拒绝猜测时使用：

```
python -m tpc doctor --dir <数据目录>
```

它扫描目录下所有节点日志（容忍坏行并逐行报告），汇总每个事务的证据：
协调者决议、各参与方投票、各参与方已持久化的决议、冲突记录，然后给出建议：

| 证据 | 建议 | 置信度 |
|---|---|---|
| 同时存在 commit 与 abort 证据 | **不给建议**，要求人工介入 | none |
| 仅有 commit 证据 | commit | high |
| 仅有 abort 证据 | abort | high |
| 无任何决议证据，但有 prepared | abort（presume abort 安全） | medium |
| 事务未到达任何参与方 | abort（无事可撤） | low |

存在损坏日志时置信度自动降一级并附加警示。**doctor 不写任何文件，
不自动应用任何决议**——最终动作必须由人确认后执行（例如修复日志后用
`python -m tpc recover --dir D` 完成恢复）。

## 故障注入测试（tests/test_tpc.py）

- 正常提交 / 投 no 全量回滚
- 参与方在 PREPARE 前崩溃 → 超时回滚
- 参与方投票后崩溃/被分区 → 保持阻塞，分区恢复后收敛到 abort
- 参与方重启后 PREPARED 状态从日志恢复
- 协调者在决议持久化后被强杀 → 重启完成提交；决议前被杀 → presume abort
- 协调者连续被杀 3 次仍收敛
- 丢包 20% + 重复 30% + 随机延迟乱序下完成提交
- 重复 DECISION 幂等（日志不重复记录）；矛盾决议被拒绝并记录 conflict
- doctor：损坏日志 + commit 证据 → 建议 commit；无决议证据 → 建议 abort；
  矛盾证据 → 拒绝建议
- 30 个随机种子的模糊测试：随机丢包/重复/延迟/分区/多节点随机崩溃重启，
  每轮断言全网决议一致且事务最终终止
