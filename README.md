# tpc — 两阶段提交（2PC）库与工具

纯 Python 3 标准库实现，无第三方依赖。包含：协调者/参与方状态机库、
可注入故障的模拟通道、命令行工具、故障注入测试集、只读恢复工具。

## 运行测试

```
python3 -m unittest discover -s tests -v
```

## 命令行用法

运行一次事务（可注入丢包/重复/延迟与节点强杀）：

```
python3 -m tpc run --dir /tmp/demo --txn T1 --parts p1,p2,p3 \
    --seed 3 --drop 0.1 --dup 0.2 --max-delay 1.5 --crash coord@2:6
# --crash coord@2:6 表示 t=2 强杀协调者, t=6 重启恢复; 可多次指定
# --no p2 让 p2 投 no
```

从日志重启所有节点并完成未完成的事务：

```
python3 -m tpc recover --dir /tmp/demo
```

查询各参与方的阻塞（已 prepared 未收到决议）清单：

```
python3 -m tpc blocked --dir /tmp/demo
```

协调者日志损坏时的只读恢复报告（只给证据与建议，不自动修改）：

```
python3 -m tpc doctor --dir /tmp/demo
```

## 库用法

```python
from tpc import Coordinator, Participant, Sim, Faults

sim = Sim(seed=1, faults=Faults(drop=0.1, dup=0.1, max_delay=2.0))
coord = Coordinator("coord", "/tmp/data")
parts = [Participant(f"p{i}", "/tmp/data") for i in range(3)]
coord.start(sim)
for p in parts: p.start(sim)
coord.begin(sim, "T1", [p.name for p in parts])
sim.run(60)
print(coord.txns["T1"].decision, parts[0].blocked())
```

## 文档

状态机、持久化时机、恢复流程与一致性论证见 [docs/design.md](docs/design.md)。
