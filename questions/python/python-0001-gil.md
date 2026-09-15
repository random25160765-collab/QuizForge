---
id: python-0001
type: single
topic: python-runtime-gil
chapter: 运行时与并发
tags: [GIL, 多线程, 多进程, CPU 密集]
difficulty: 2
source: "CPython 文档 / PEP 703"
---

在 **CPython 3.12**（较新版本仍默认持有 GIL）上，有一段纯计算代码需要对一个列表的
上百万个元素做数值变换。下面哪种说法是正确的？

```python
def work(chunk):
    s = 0
    for x in chunk:
        s += x * x + 3 * x - 7
    return s
```

## 选项
- A. 用 `threading` 起 4 个线程分块计算，可以获得接近 4 倍的加速
- B. 用 `threading` 起 4 个线程几乎不会加速，因为 GIL 使得同一时刻只有一个线程在执行 Python 字节码；要并行 CPU 计算应改用 `multiprocessing` 或 `concurrent.futures.ProcessPoolExecutor`
- C. GIL 只在 I/O 操作时释放，纯计算代码完全不受影响，因此多线程与多进程性能一致
- D. CPython 没有 GIL，Python 的多线程天然是可并行执行的，只是受全局解释器锁语义限制的是 C 扩展

## 答案
B

## 解析
GIL（Global Interpreter Lock）是 CPython 解释器里的一把全局互斥锁：
**任意时刻只允许一个线程持有它并执行 Python 字节码**。因此纯计算的 Python 代码
在多线程下是**串行**的，起 4 个线程不会带来加速，反而因为线程切换而略有开销。

几个关键事实，逐项对照：

**A 错。** 纯 CPU 计算的多线程在 CPython 上不会并行。加速比接近 1，甚至因为
GIL 争抢与切换而略低于 1。

**B 对。** 因为进程各自有独立的解释器与 GIL，`multiprocessing` /
`concurrent.futures.ProcessPoolExecutor` 是绕过 GIL 做 CPU 并行的标准方案。
代价是：进程启动开销、参数与结果需要**序列化（pickle）传输**，共享大对象时要
考虑 `shared_memory`。

```python
from concurrent.futures import ProcessPoolExecutor
with ProcessPoolExecutor(max_workers=4) as ex:
    total = sum(ex.map(work, chunks))
```

**C 错。** GIL 的确会在阻塞式 I/O 时被主动释放（这是「I/O 密集用多线程有效」的原因，
例如 `socket.recv`、`time.sleep`、文件读写），但「只在 I/O 时释放」这个前提是错的：
解释器还会按 `sys.setswitchinterval()`（默认 5 ms）做**周期性释放**，
以便其它线程有机会运行。这个周期切换机制让「纯计算多线程完全不切换」的说法不成立，
但结论仍是串行执行。

**D 错。** CPython 有 GIL；PyPy 也有；Jython 与 IronPython 没有。
PEP 703 提出的自由线程（free-threaded）构建在 3.13 起作为实验性选项提供
（`python3.13t`），但**默认构建仍然带 GIL**，这是理解这道题的前提。

顺带一个常被问到的等价问题：**为什么 C 扩展能绕开 GIL？**
因为扩展在调用 `Py_BEGIN_ALLOW_THREADS` / `Py_END_ALLOW_THREADS` 之间会主动释放 GIL。
NumPy 的重算子、`hashlib`、压缩库都是这么做的——这也是为什么「用 NumPy 向量化」
「把热点下沉到 C 扩展」能在单线程下就拿到大幅加速的原因。
