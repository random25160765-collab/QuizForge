---
id: nvidia-arch-0001
type: single
topic: nvarch-sm-sched
chapter: SM 内部结构
tags: [SM, sub-partition, warp scheduler, register file]
difficulty: 3
source: "NVIDIA CUDA C++ Programming Guide §11.1 / GA100 Whitepaper"
---

关于从 Volta 起延续到 Hopper / Blackwell 的 SM（Streaming Multiprocessor）内部结构，
下列哪一项描述最准确？

## 选项
- A. 一个 SM 是一个统一的调度单元，所有 warp 共享一个调度器与一份寄存器堆
- B. 一个 SM 被划分为 4 个 processing block（sub-partition），每个有独立的 warp 调度器与寄存器堆；寄存器总量决定了 SM 上可常驻的线程数上限之一
- C. 一个 SM 上的 64 个 warp 每周期全部可以发射指令，因此占用率越高 IPC 一定线性越高
- D. 只读的常量缓存与纹理单元不存在于 SM 内部，而是挂在 L2 上由所有 SM 共享

## 答案
B

## 解析
**正确项 B。** 从 Volta 起（GV100、GA100、GH100、GB100 一路延续），一个 SM 在结构上被
划分为 **4 个 processing block（也叫 sub-partition）**：

* 每个 sub-partition 有自己的 **warp scheduler** 与 **dispatch unit**；
* 每个 sub-partition 有自己的 **register file** 分割；
* 一个 warp 一旦被分配到某个 sub-partition，就**固定驻留在那里**，不会迁移。

一个 sub-partition 每周期最多发射 1 条指令（个别架构上某些指令可以双发射），
因此「SM 的峰值发射宽度」是 4 条/周期，而不是 64 条。

逐项辨析：

**A 错。** 调度器与寄存器堆都是**按 sub-partition 划分**的，不是 SM 级共享。
这正是「寄存器用量决定 occupancy」的机制来源：每个线程用掉的寄存器属于它所在
sub-partition 的寄存器堆，寄存器耗尽就没法再放入新 warp。

**C 错。** 三个原因：

1. 发射宽度是 4 条/周期（4 个调度器各 1 条），不是 64；
2. 大部分指令有延迟（如全局访存几百个周期），需要足够多的就绪 warp 来填补，
   但调度器每周期只能挑一个；
3. 占用率与 IPC 是**边际递减**关系：一旦延迟已经被掩盖，再加 warp 不会提高 IPC，
   反而可能因为 L2 / 显存带宽或访存队列拥塞而恶化。

**D 错。** 常量缓存（constant cache）、纹理/只读数据路径都在 **SM 内部**
（`__constant__` 与 `__ldg` 走的就是这些路径），L1 / shared memory 也是 SM 内的
可配置分割（如 A100 上是 192 KB 的 L1/shared 组合）。L2 才是所有 SM 共享的那一层。

一个实用推论：**warp 数不是越多越好，而是要和「每个线程的寄存器占用 × 目标 occupancy」
一起算**。例如每个 sub-partition 有 65536 个寄存器、每个线程用 32 个寄存器，
那么每个 sub-partition 最多容纳 2048 个线程，即 64 个 warp —— 正好是该架构的
warp 上限之一。想再提高 occupancy 只能降低每线程寄存器用量。
