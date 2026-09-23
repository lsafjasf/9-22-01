# minivm — 小型脚本语言字节码虚拟机 + 分代垃圾回收器

纯 Python 3 标准库实现，无第三方依赖。包含：栈式字节码 VM、
分代 GC（年轻代复制 / 老年代标记清除 + 写屏障）、汇编器、CLI、
示例脚本与测试。

## 运行

```bash
# 运行示例脚本（自动打印 GC 统计）
python3 -m minivm run examples/closures.asm     # 闭包捕获
python3 -m minivm run examples/trap.asm         # 错误中断 (TRAP/RAISE)
python3 -m minivm run examples/cycles.asm       # 循环引用回收 + 释放钩子
python3 -m minivm run examples/crossgen.asm     # 跨代引用（写屏障）
python3 -m minivm run examples/longrun.asm      # 长时间运行：内存不增长

# 可调堆参数
python3 -m minivm run examples/longrun.asm --young-size 512 --old-threshold 2048

# 全部测试（闭包 / 循环引用 / 跨代引用 / 错误中断 / 内存稳定性）
python3 -m unittest discover -s tests -v
```

## 指令集（栈式 VM）

见 `minivm/bytecode.py` 头部注释。要点：

- **常量池**：每个函数一个常量池（`CONST k`），池中的堆引用（字符串）
  作为 GC 根被精确标记。
- **局部变量**：`LOAD_LOCAL / STORE_LOCAL`，局部变量在值栈上（帧基址 + 槽位）。
- **闭包捕获**：`CLOSURE f L0 U1` 按引用捕获父帧局部变量（L）或父闭包
  upvalue（U）；`LOAD_UPVALUE / STORE_UPVALUE` 读写。帧返回时 open
  upvalue 关闭（值拷贝进堆对象），Lua 风格。
- **调用/返回**：`CALL n`（闭包或原生函数）、`RETURN`。
- **条件跳转**：`JUMP / JUMP_IF_FALSE / JUMP_IF_TRUE label`。
- **错误中断**：`TRAP handler` 压入错误处理器；运行时错误（除零、类型
  错误、越界）或 `RAISE` 展开到最近处理器并压入错误值；`UNTRAP` 弹出。

汇编格式见 `minivm/assembler.py`  docstring（`fn/end`、标签、字面量自动
进入常量池）。

## 堆与对象头

`minivm/objects.py`：每个堆对象带对象头 —— 代（young/old）、年龄、
标记位、复制转发指针、资源释放钩子。对象类型：`HObj`（对象）、
`HArray`（数组）、`HStr`（字符串）、`HClosure`（闭包）、`HUpvalue`。

## 精确可达性

GC 根由 VM 以 `(container, key)` 槽位形式注册（`VM._root_slots`），
复制式回收会**回写**这些槽位：

- 值栈（临时值、各帧局部变量、被调闭包槽位）
- 全局变量表
- 每个函数的常量池
- open upvalue 列表

每个对象类型通过 `_pointer_slots` 精确声明哪些字段是指针，无保守扫描。

## 分代回收

- **年轻代（复制）**：半空间 bump 分配，Cheney 算法 BFS 复制存活对象，
  所有引用（含 VM 根）被重写到新地址；幸存 `promote_age` 次后晋升老年代。
- **老年代（标记清除）**：从全部根出发标记（年轻代只遍历不清扫），
  清扫未标记对象并运行释放钩子。老年代超过阈值时触发，阈值自适应增长。
- **跨代引用**：minor GC 额外扫描记忆集（remembered set）中的老对象，
  把它们当作根；晋升对象若指向年轻对象也会进入记忆集。

### 写屏障何时触发

`Heap.write_barrier(container, value)` 在**每一次向堆对象写入指针**之后
调用：`SET_FIELD`、`SET_INDEX`、`ARR_PUSH`、`STORE_UPVALUE`（写入已关闭
的 upvalue）以及原生函数中的存储。仅当 **container 为老年代且 value 为
年轻代** 时把 container 记入记忆集 —— 其余情况（写入年轻对象、写入
立即数）无需屏障。minor GC 扫描记忆集，保证老→年轻引用不漏标记。
`tests/test_gc.py` 中有专门测试：无屏障时老对象指向的年轻对象会被
误回收（`test_without_barrier_young_child_would_be_lost`），有屏障则存活。

## 循环引用与资源释放钩子

标记清除与 Cheney 复制都基于可达性而非引用计数，循环垃圾自然回收
（`examples/cycles.asm`）。`on_finalize(obj, tag)` 原生函数给对象挂
释放钩子，对象被回收时钩子触发一次（复制/晋升时钩子随对象头保留，
见 `Heap.collect_minor`）。

## 统计输出

每次运行结束打印：各代回收次数与暂停时间、最大暂停、分配/释放对象数、
当前可达对象数（final full GC 后的精确值）、峰值存活数、max RSS 及增量。

## 内存不增长的证明

`examples/longrun.asm`：10 万次迭代，每次分配一个自引用对象 + 一个数组，
仅保留最近 64 个（环形缓冲，老→年轻写入触发写屏障），每 97 个对象挂
释放钩子，每 5000 次脚本内主动 full GC。实测（`--young-size 1024`）：

| 迭代 | 分配对象 | minor/major 次数 | 峰值存活 | max RSS |
|------|----------|------------------|----------|---------|
| 10万 | 200,012 | 241 / 21 | 1,134 | 15.9 MB |
| 50万 | 1,000,012 | 1201 / 101 | 1,134 | 16.2 MB |

分配量放大 5 倍，峰值存活与 RSS 基本不变 —— 堆有界，内存不持续增长；
全部 5155 个释放钩子最终都被调用（`tests/test_vm.py::LongRunTest` 断言）。

## 目录结构

```
minivm/
  objects.py     对象头与堆对象类型
  heap.py        分代堆：复制 + 标记清除 + 写屏障 + 记忆集 + 统计
  bytecode.py    指令集定义与代码对象
  assembler.py   文本汇编器（标签、常量池自动 intern）
  vm.py          栈式 VM：帧、闭包/upvalue、TRAP/RAISE、原生函数
  cli.py         命令行入口与统计输出
examples/        闭包 / 错误中断 / 循环引用 / 跨代引用 / 长时间运行
tests/           GC 单元测试、VM 测试、示例集成测试
```
