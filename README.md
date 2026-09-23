# eventorder — 因果一致的事件打戳与多源合并

纯 Python 3 标准库实现，无第三方依赖。

## 缺陷复现（修复前）

`eventorder/legacy.py` 保留了原始实现：仅用本机墙上时间（毫秒）打戳，
合并时只按 `ts` 排序。`tests/test_reproduce.py` 用**可注入的脚本时钟**
（不 sleep、不依赖真实计时）稳定复现四类缺陷：

1. **时钟回拨**：B 实际发生在 A 之后，但 B 的墙上时间更早 → B 排到 A 前。
2. **同一毫秒多事件**：多个事件 `ts` 相同，结果只取决于流的传入顺序
   （稳定排序的偶然结果），换个调用顺序全序就变。
3. **重启后倒退**：进程在时钟偏慢的机器上重启，新事件时间戳小于旧事件。
4. **跨来源合并违反因果**：s2 收到 s1 的事件 A 后才产生 B，但 s2 钟慢，
   B 被排到 A 前。

## 修复机制：为什么是 HLC（+ origin + seq）

每个事件除原始墙上时间 `ts` 外，新增全序键

```
(hlc_l, hlc_c, origin, seq)
```

- `hlc_l`：混合逻辑时钟（Hybrid Logical Clock, Kulkarni et al. 2014）的
  物理部分，跟踪本来源“已知的最大物理时间”；
- `hlc_c`：逻辑计数器，同一 `l` 内的事件靠它严格区分先后；
- `origin`：来源的**唯一身份**（持久化 UUID 或显式指定），与仅作展示的
  人类可读标签 `source` 分离，允许标签重复；
- `seq`：每个 origin 内单调递增的序列号，参与并列裁决。

**为什么不只用墙上时间 + 加重试排序**：墙上时间不是因果信号——回拨、DST、
重启、钟偏都会让它倒退，重试无法恢复“谁导致了谁”的信息。

**为什么不是纯 Lamport 时钟**：Lamport 计数器与物理时间脱钩，长时间运行后
与真实时间差任意大，审计/展示时间线失去意义；且合并真实系统时无法判断
“这个事件大概发生在什么时候”。HLC 的 `l` 始终夹在
`max(本地钟, 见过的最大 l)` 附近（偏差有界，约为时钟偏斜），既携带因果又
贴近墙上时间。

**因果注入点**：来源 s2 在处理 s1 的事件前调用 `clock.observe(event)`，
把对方的 `(l, c)` 折入本地时钟；此后 s2 打出的任何戳都严格大于该事件。
未交互过的事件是并发事件，由 `(l, c, origin, seq)` 字典序给出**唯一、可
重现**的全序，与进程布局、调用顺序、流的分片方式无关。

**重启不回退**：`HybridClock(state_path=...)` 在构造时加载、每次打戳后以
“临时文件 + fsync + 原子 rename”持久化 `(origin, l, c, seq)`。重启即使
墙上时间倒退，时钟也从已持久化的 HLC 继续前进。

**接口兼容**：`merge(*streams)` 的调用形状与旧版一致（接收列表、返回
列表）；事件上的 `ts`（原始墙上时间，可为 `None`）与 `source` 原样保留，
新增字段不覆盖旧字段。

### 代价（明确列出）

- 每条事件多存 4 个字段（两个整数、一个来源 id、一个序列号），约几十字节；
- 跨来源因果**必须**在接收路径上显式调用一次 `observe()`——未折入的因果
  关系系统无从得知（这与向量时钟等所有逻辑时钟的要求一致）；
- 启用持久化时每次打戳有一次原子小文件写入（可按需批量/异步 fsync 调优）；
  状态文件丢失等同于新 origin：仍有确定全序，但无法声明跨重启因果；
- HLC 的 `l` 依赖物理时钟有界偏斜；极端偏斜下 `l` 会跟随“最快的钟”，
  这是 HLC 论文已知的标准取舍；
- 流式合并要求每条输入流自身有序（单一 origin 天然满足）；乱序到达的
  “迟到事件”需要上游水位线/窗口缓冲，不属于打戳机制能解决的范围。

## 大数据量的内存策略

- `merge(*streams)`：内存内排序，兼容旧接口，适合常规批量；
- `merge_streams(*streams)`：基于 `heapq.merge` 的 k 路流式合并，
  缓冲 O(k)（k = 流数量），与事件总数无关；
- `external_merge(*streams, chunk_size=..., directory=...)`：把各流溢写为
  排序的 NDJSON 分块文件，再 k 路堆合并，峰值内存 O(chunk_size + 分块数)，
  磁盘代价约为一份临时拷贝，结束后自动清理。

## 边界行为

- **时间戳缺失**：事件没有 `ts` 时写入 `None` 并照常按 HLC 排序；已有
  `ts`（含显式 `None`）永不被覆盖。
- **来源标识重复**：排序身份是持久化的唯一 `origin`，`source` 只是标签，
  两个同名来源不会冲突；未配置状态文件时自动生成 UUID origin。
- **未打戳事件参与合并**：`merge`/`merge_streams` 直接抛 `ValueError`，
  避免静默产生不可信顺序。

## 运行

```bash
# 全部回归 + 复现测试（确定性，无需联网/等待）
python3 -m unittest discover -s tests -v

# 新旧行为对比演示
python3 demo.py
```

## 最小用法

```python
from eventorder import HybridClock, merge_streams

order = HybridClock("orders", state_path="/var/lib/eventorder/orders.state")
bill  = HybridClock("billing", origin="billing-f4a2")

e1 = order.stamp({"id": "order-1", "ts": wall_ms_or_None})
bill.observe(e1)                 # 声明因果：账单由该订单产生
e2 = bill.stamp({"id": "bill-1"})

for event in merge_streams(stream_a, stream_b):  # O(k) 内存
    dispatch(event)
```
