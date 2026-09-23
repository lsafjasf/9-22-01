# colstore — 列式存储与查询执行库（纯标准库 Python 3）

面向"记录数远超内存、查询只涉及少数列"的分析场景。仅使用 Python 标准库。

## 结构

- `colstore/format.py` — 落盘格式：列块、字典编码、游程编码、空值位图、块统计索引
- `colstore/engine.py` — 执行引擎：谓词下推、两阶段分组聚合 / 排序 / 去重（超预算溢写磁盘）
- `colstore/naive.py` — 朴素逐行参考实现（用于对拍）
- `tests/test_differential.py` — 与朴素实现对拍的正确性测试
- `bench.py` — 性能与内存基准

## 落盘格式

表是一个目录：`meta.json`（schema、块行数、总行数）+ 每列一个 `col_<i>.bin`。

列文件：`[magic][block]*[JSON 块索引][索引长度][magic]`。

块：`[1B 编码][4B 行数][4B 空值数][4B 位图长][空值位图][负载]`。
负载只存非空值，三种编码按实际字节数逐块择优：

- `PLAIN` 值紧密排列；
- `DICT` 字典 + 1/2/4 字节定宽码（字典膨胀时自动回退 PLAIN）；
- `RLE` `(值, 游程长)` 对（极端偏斜时收益最大）。

块索引记录 `(offset, len, rows, nulls, enc, min, max)`，因此：

- **谓词下推**：用 min/max/nulls 判定整块不可能匹配即跳过，不读盘；
- **随机访问**：点查 / 范围扫描按行号二分定位块，只解压覆盖的块，绝不整列解压；
- **列裁剪**：只打开查询涉及的列文件。

## 两阶段执行与溢写

- **阶段 1（部分结果）**：流式读取列块 → 块级剪枝 → 行级谓词 → 更新哈希聚合表 /
  排序缓冲。哈希表条目数（或排序缓冲行数）超过 `memory_budget` 时，按键排序后溢写临时文件。
- **阶段 2（合并）**：对所有有序溢写文件 + 内存残余做 k 路归并，相同键的聚合态用
  可交换可结合的 combine 函数合并（`avg` 以 `(sum, count)` 传递）。
- 聚合支持 `count / sum / min / max / avg`，遵循 SQL 空值语义（聚合忽略 NULL，
  空集上 `sum/min/max/avg` 为 NULL，`count` 为 0）。
- `distinct` 复用 group-by 框架；`sort` 为外排序（有序 run + k 路归并），NULL 排最后。

## 运行

```bash
python3 -m unittest discover -s tests -v   # 对拍正确性测试（14 个用例）
python3 bench.py                            # 性能与内存基准
```

## 最小示例

```python
from colstore import TableWriter, TableReader, pred
from colstore import engine

w = TableWriter("/tmp/t", [("ts", "int"), ("country", "str"), ("amount", "float")])
w.extend([(1, "cn", 9.5), (2, "us", None), (3, "cn", 4.0)])
w.close()

r = TableReader("/tmp/t")
info = {}
res = engine.group_by(r, ["country"], [("count", None), ("sum", "amount")],
                      [pred("ts", "ge", 2)], memory_budget=1000, info=info)
print(res, info, r.bytes_read)   # info 含 blocks_skipped / blocks_read / spills
print(r.point(1, ["country"]))   # 点查：只解码覆盖块
```

## 实测数据（Python 3.12，300k 行 × 6 列主数据集，块 8192 行）

| 场景 | 结果 | 耗时 | 内存峰值 | 备注 |
|---|---|---|---|---|
| 写入 | 7.9MB 落盘 | 0.45s | — | 原始约 13.7MB |
| Q1 选择性过滤 | 1245 行 | 0.021s | 1.2MB | 读 2 块 / 跳 35 块，实读 231KB；朴素逐行 0.232s（11×） |
| Q2 分组聚合（不溢写） | 32 组 | 1.49s | 1.3MB | — |
| Q2 分组聚合（预算=8 组） | 32 组 | 1.43s | 1.3MB | 溢写 37 次，结果与不溢写完全一致 |
| Q3 高基数组（15.5 万组，预算 2000） | 155305 组 | 5.35s | 33.9MB | 不溢写时 69.4MB，溢写省一半内存 |
| Q4 排序（预算 2 万行） | 30 万行 | 3.41s | 38.4MB | 溢写 12 段；内存内 59.9MB |
| Q5 点查 | — | ~2.4ms/行 | — | 每次只解码 1 个块；范围扫描 500 行 6ms |
| 极端偏斜（99.9% 同值） | RLE | 0.92s | 185KB | 4.6MB 原始 → 2.3MB（含另一列） |
| 字典膨胀（10 万唯一字符串） | 回退 PLAIN | 0.028s | — | 点谓词读 1 块 / 跳 12 块，实读 235KB |
| 超宽表（200 列查 2 列） | 50 组 | 0.083s | 552KB | 实读 393KB，仅占全表 1.0% |
| 空值密集（40% NULL） | 101 组 | 1.26s | 770KB | 位图过滤 + notnull 下推 |

边界覆盖：全 NULL 列（统计直接剪掉全部块）、空表 / 单行表、块边界行数、
字典膨胀回退、极端偏斜 RLE、200 列宽表列裁剪、1~100 的极小溢写预算。
