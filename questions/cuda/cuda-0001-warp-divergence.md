---
id: cuda-0001
type: single
topic: cuda-exec-warp
chapter: 执行模型与 warp
tags: [warp, 分支发散, SIMT, 占用率]
difficulty: 3
source: "CUDA C++ Programming Guide, §5.4.2 / §11 SIMT Architecture"
---

一个 block 有 128 个线程。下面的 kernel 在 block 内存在明显的分支发散，
请判断它对**整个 block 的执行时间**的影响，选出最准确的一项。

```cuda
__global__ void branchy(float* out, const float* in, int n) {
    int tid = blockIdx.x * blockDim.x + threadIdx.x;
    if (tid % 2 == 0) {
        for (int i = 0; i < 200; ++i) out[tid] += in[tid] * 0.5f;
    } else {
        out[tid] = in[tid];
    }
}
```

## 选项
- A. 同一 warp 内两条分支会各执行一次，`if` 分支的 200 次循环与 `else` 分支串行叠加，实际耗时约为不分支版本的 2 倍
- B. 因为 `tid % 2` 在同一个 warp 内交替，每个 warp 都会分裂成两条路径串行执行；`if` 分支的循环占主导，因此每个 warp 的耗时约等于「一条 200 次循环」，吞吐约为理想情况的 50%
- C. 现代 GPU 支持独立线程调度（Independent Thread Scheduling），同一 warp 的线程可以并发推进，因此两条分支的代价可以完全重叠，没有性能损失
- D. 分支发散只影响寄存器分配，不影响执行时间，warp 会被自动拆成两个 sub-warp 并行执行

## 答案
B

## 解析
一次 `if/else` 在 SIMT 模型下的执行过程是：
**把 warp 里走 `if` 的线程设为活跃、走 `else` 的屏蔽掉，执行一遍 `if` 体；
再反过来执行一遍 `else` 体。** 两条路径是**串行**的，不是并行的。

在这道题里：

* `tid % 2` 在连续的 thread index 上交替，因此**每一个 warp 内部都会发散**；
* `if` 分支含 200 次迭代的循环，`else` 分支只有一条赋值——两者代价悬殊。

于是每个 warp 的耗时 ≈ $T_{\text{loop}} + T_{\text{assign}} \approx T_{\text{loop}}$，
而理想（无发散）情况下 32 个线程本可以同时完成循环，耗时同样是 $T_{\text{loop}}$。
但**有效吞吐**只有一半：32 个线程里每次只有 16 个在执行循环。
换句话说，耗时没有变成 2 倍，但**计算资源的利用率掉了一半**。

逐项看：

* **A 错**：耗时不是简单相加成 2 倍。串行执行的是两条分支的**指令流**，而各自只有一半通道活跃，
  总时间约等于较慢那条分支的时间，不是两条时间之和。
* **B 对**：准确描述了「时间≈最慢分支」与「吞吐≈50%」这两件事。
* **C 错**：Independent Thread Scheduling（Volta 起）允许 warp 内线程在不同点等待、
  由调度器交织推进，并提供了 `__syncwarp()` 与 warp 级原语来配合；但它**不会**让两条
  互斥分支的指令并行发射。同一时刻 SM 的一个 sub-partition 仍然只发射一条指令，
  发散带来的序列化代价依然存在。
* **D 错**：GPUs 不会把发散 warp 拆成并行执行的 sub-warp。寄存器分配与执行时间都受影响，
  但原因是串行化的指令流，而不是「自动拆分」。

消除这类发散的标准手法是**让分支在 warp 内一致**：把数据按奇偶重排
（`tid < 16` 与 `tid >= 16` 分别处理两段），或者干脆把两条路径改成谓词化/predicated 形式，
使两条路径代价相近时可以用 `select` 消除控制流分歧。
