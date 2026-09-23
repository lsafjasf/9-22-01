# 事件排序库：乱序缺陷修复

## 问题

原实现（`event_order_buggy.py`）直接用本机墙上时间戳排序。以下场景会违反因果：

1. **时钟回拨**（NTP 校正/手动改时）：后发生的事件拿到更小的时间戳；
2. **同一毫秒多事件**：时间戳相同，先后关系丢失；
3. **进程重启**：墙上时间倒退，新事件排到旧事件之前；
4. **跨来源合并**：各机器时钟有偏差，"A 发生后 B 才发生"的顺序被打乱。

复现测试见 `test_repro.py`，每个用例同时断言旧实现乱序、新实现有序。

## 修复：混合逻辑时钟（HLC）

排序键改为三元组 `(physical_ms, logical, node)`（`event_order.py`）：

- `physical_ms`：单调不减的物理毫秒。时钟回拨时冻结在上次观测值，绝不倒退；
- `logical`：同一物理毫秒内的递增计数，区分同刻事件；
- `node`：来源唯一标识，为并发事件提供确定且可重现的全序仲裁。

跨来源因果通过 `EventClock.observe()` / `stamp(..., after=事件)` 传递：
观察到外部事件后，本地 HLC 推进到对方之后，因此"被外部观察到先后发生"的事件
排序后先后关系不变。并发事件按 `(physical, logical, node)` 全序排列，结果确定可重现。

### 为什么选 HLC 而不是别的

- **不是排序+重试**：重试无法表达跨来源因果；回拨后新时间戳仍可能小于已发出的事件。
- **不是 Lamport 时钟**：只有一个逻辑计数，与物理时间完全脱钩，无法用于展示，
  也无法限制长期空闲节点间的时间戳漂移。
- **不是向量时钟**：每事件开销 O(来源数)，且只给偏序，仍需额外仲裁才能得到全序；
  HLC 每事件固定 3 个字段、O(1) 比较，同时给出全序。
- **HLC 的 physical 分量贴近墙上时间**，展示/审计时语义自然，且满足：
  若事件 e 因果先于 f，则 `hlc(e) < hlc(f)`。

### 代价

- 每事件额外 `(int, int, str)` 三个字段（`Event` 用 `__slots__` 控制内存）；
- 跨来源因果需要显式传递时间戳（消息里带上 HLC，接收方 `observe()`）；
- 重启后的单调性依赖 `state_path` 持久化，每次打戳一次小文件原子写
  （可用 `persist_every=N` 降低频率，代价是崩溃后最多丢失 N 次递增，
  仍不违反单调性，只是 physical 可能停在崩溃前）；
- HLC 只保证"被观察到的因果"；从未通信的并发事件按确定规则排序，不声称有因果。

## 边界与规模

- **时间戳缺失**：`wall_time=None` 合法，仅影响展示（`display_time` 退化为 HLC
  物理分量）；无 HLC 的旧事件在 `sort_events`/`merge_streams` 中按被观察到的
  顺序补戳，先被看到的一定排前。
- **来源标识重复**：`merge_streams` 检测重复并告警，按注册顺序消歧，结果确定可重现。
- **海量事件**：`merge_streams` 是惰性 k 路归并（`heapq`），内存 O(来源数)，
  与事件总数无关，可处理无限流；`Event` 使用 `__slots__`。

## 接口兼容

`Event` / `stamp` / `sort_events` / `merge_streams` 名称与旧版一致；
`Event.timestamp` 属性保留（映射到 `wall_time`），原始墙上时间戳始终保留用于
展示与审计，只是不再参与排序。

## 运行

```bash
cd /home/administrator/gsb/uid14/B
python3 -m unittest test_repro -v       # 缺陷复现（4 个场景）
python3 -m unittest test_regression -v  # 回归测试（因果/确定性/边界/内存/兼容）
python3 -m unittest discover -v         # 全部
```

仅依赖 Python 3 标准库。
