# 两阶段提交状态机与恢复流程

本文档描述 `two_phase_commit` 包中协调者与参与方的完整状态机、每条记录的
持久化时机，以及崩溃/恢复为何不会产生分歧决议。

## 1. 协议消息

| 消息 | 方向 | 含义 |
| --- | --- | --- |
| `PREPARE` | 协调者 → 参与方 | 阶段 1：请对事务投票 |
| `VOTE_COMMIT` / `VOTE_ABORT` | 参与方 → 协调者 | 投票结果（投票前已落盘） |
| `COMMIT` / `ABORT` | 协调者 → 参与方 | 全局决议（决议前已落盘） |
| `ACK_COMMIT` / `ACK_ABORT` | 参与方 → 协调者 | 决议已在该方应用 |
| `STATUS_QUERY` | 参与方 → 协调者 | 恢复协议：查询事务决议 |
| `DECISION_COMMIT/ABORT/UNKNOWN` | 协调者 → 参与方 | 对查询的应答 |

## 2. 持久化记录与时机

日志为仅追加、CRC 校验、每次写入 `flush + fsync` 的二进制帧（见
`storage.py`），记录类型：

`BEGIN`、`VOTE`、`DECISION`、`APPLY`、`ACK`、`END`。

关键顺序（**先持久化、后发送**，任何一步都不允许颠倒）：

1. 协调者 `BEGIN` fsync 完成后才广播 `PREPARE`。
2. 参与方把 `VOTE(VOTE_COMMIT)` fsync 完成后才发送 `VOTE_COMMIT`；
   一旦投赞成票，资源即进入“不可单方面放弃”的保留状态。
3. 协调者做出 `COMMIT/ABORT` 决议时，先把 `DECISION` fsync，
   **然后**才向任何参与方发送决议消息。这是安全性的核心不变量。
4. 参与方收到 `COMMIT`：先执行资源效果，再写 `APPLY(COMMIT)` 并 fsync，
   然后才回 `ACK_COMMIT`。`APPLY` 记录就是幂等键，崩溃恢复不会重复执行
   效果。`ABORT` 对称。
5. 协调者收齐全部 ACK 后写 `END`（仅用于垃圾回收/可观测性）。

> “效果在 `APPLY` 前执行、记录紧随其后”在进程被 `kill -9` 的模型下是
> 安全的：若效果发生但记录未持久化，重启后参与方仍处 PREPARED，会通过
> `STATUS_QUERY` 重新拿到同一个 COMMIT；资源回调本身要求按事务号幂等，
> 第二次调用不会重复应用（测试里用“每个事务一个标记文件”来体现）。

## 3. 协调者状态机（每事务）

```
                 begin_txn()
                     |
                 [BEGIN fsync]
                     v
        +------- WAIT（收票/重试 PREPARE）
        |          |  \
        |          |   \__ 任一 VOTE_ABORT 或 prepare 超时
        |          v       v
        |   全票 VOTE_COMMIT   [DECISION(ABORT) fsync]
        |          |                  |
        |   [DECISION(COMMIT) fsync]  |
        |          v                  v
        |        COMMIT             ABORT ---- 重发决议直到收齐 ACK
        |          |                  |        （参与方幂等处理重复命令）
        |          +--------+---------+
        |                   v
        |                 [END] --> DONE
        |
        +-- 重启恢复时若仍在 WAIT：推定中止（presumed abort）
```

- **WAIT**：周期性向“尚未收到投票”的参与方重发 `PREPARE`（重复 PREPARE
  会被参与方用重复投票幂等应答）；同时有独立的 prepare 超时定时器。
- **COMMIT / ABORT**：周期性向“尚未 ACK”的参与方重发决议；重发有界，
  但参与方任何时刻都可主动 `STATUS_QUERY`，决议记录长期保留可读。
- **DONE**：所有参与方确认，写 `END`。
- 投票/ACK 均用集合去重；迟到的错误类型 ACK 会触发重发真实决议。

### 协调者重启恢复（`Coordinator._recover`）

重放日志前缀（遇到损坏帧直接拒绝启动，绝不截断或猜测）：

1. 有 `DECISION(COMMIT/ABORT)`：这是权威历史，按它重发决议。
2. 没有 `DECISION`（崩溃发生在 WAIT）：采用**推定中止**——立刻持久化
   `DECISION(ABORT)` 后驱动阶段 2。安全性依据：系统里从未发出过任何
   COMMIT（发送严格发生在 `DECISION` fsync 之后），因此没有任何参与方
   可能已经提交。
3. 有 `END`：无事可做，但仍能对 `STATUS_QUERY` 回答最终决议。

## 4. 参与方状态机（每事务）

```
(无记录) --PREPARE--> 评估资源
   |                     |
   |               可提交 | 不可提交
   |          [VOTE_COMMIT fsync]   [VOTE_ABORT + APPLY(ABORT) fsync]
   |                     v                    v
   |                PREPARED -------------- ABORTED --ACK_ABORT-->
   |   (阻塞；周期 STATUS_QUERY)                  ^
   |       /      |                              |
   |  COMMIT      ABORT（不可能来自正常决议；     |
   |     |        若出现说明部署异常，记录冲突）   |
   |     v                                         |
   | 执行效果 + [APPLY(COMMIT) fsync]              |
   v     |                                         |
COMMITTED + ACK_COMMIT                             |
                                                     |
(无记录) --ABORT--> 推定中止：补记 VOTE_ABORT ------+
(无记录) --COMMIT--> 补记 VOTE_COMMIT 后正常提交（协调者权威）
```

- **PREPARED 即“阻塞态”**：在收到明确决议前必须一直保持，
  `Participant.blocked()` 返回当前阻塞清单；恢复时从日志重建该状态并
  自动开始 `STATUS_QUERY` 轮询。
- 重复 `COMMIT/ABORT` 幂等：已有 `APPLY` 记录时只重发 ACK，不再执行
  资源效果。
- 重复 `PREPARE`：PREPARED 状态下重发已持久化的那张投票。
- 参与方日志损坏同样拒绝启动，由人工取证流程处理（见
  `recovery.md`）。

## 5. 为什么不可能“一部分提交、一部分回滚”

设存在分歧，则系统中必然同时出现过一条有效的 `COMMIT` 和一条有效的
`ABORT`：

- 单协调者对一个事务只在 `_persist_decision` 里、从 WAIT 出发写入一次
  `DECISION`；一旦状态离开 WAIT，再次调用直接返回，决议不可更改。
- 参与方只有在收到 `COMMIT` 时才写 `APPLY(COMMIT)`；若它本地已经
  `ABORTED`，收到 `COMMIT` 不会提交而会记录冲突并重申事实（反之亦然），
  这种冲突只会在协调者决议被人工错误覆盖时出现——本实现从不自动改判。
- 协调者发送任何 `COMMIT` 之前，`DECISION(COMMIT)` 已 fsync；重启恢复
  只会看到并沿用该记录。崩溃能丢失的只有“消息”，丢不掉“已公布的决议”。
- WAIT 中崩溃 → 推定中止时，不可能已有任何 COMMIT 流出（同上）。

测试以“所有参与方 APPLY 记录必须同属 COMMIT 或同属 ABORT，且无残留
阻塞”为核心不变量，在 60 组随机崩溃/分区/重复/乱序/丢包历史上校验
（`tests/test_chaos.py`）。
