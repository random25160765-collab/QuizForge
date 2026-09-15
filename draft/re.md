# 大题：从 SIMT 到 Tensor Core 的 GEMM 优化

**题面** 设 \(A \in \mathbb{R}^{M \times K}\)，\(B \in \mathbb{R}^{K \times N}\)，\(C \in \mathbb{R}^{M \times N}\)，计算 \(C = A \times B\)。除特别说明外，取 \(M=N=K=4096\)，输入 FP32，累加 FP32。目标硬件：NVIDIA A100（SM80），FP32 峰值约 19.5 TFLOPS，HBM 带宽约 1.5 TB/s，每 SM shared memory 上限 164 KB，L2 约 40 MB。只允许使用 CUDA C++ 与 inline PTX，不得调用 cuBLAS 或 CUTLASS 库函数。

**说明** 本大题共 20 问，分三部分。每一问的结论将用于后续问题，建议按顺序作答。部分小问需要定量计算或写出可编译的伪代码。

---

## 第一部分：SIMT FP32 路径

### 第 1 问
写出 naive kernel：每个线程计算 \(C\) 的一个元素，每次 FMA 直接从 global memory 读取 \(A\) 和 \(B\)。

1. 写出该 kernel 的完整 CUDA 代码。
2. 计算整个 kernel 的 global memory 读取总次数（用 \(M, N, K\) 表示）。
3. 代入数值，计算总 DRAM 流量与理想流量（\(A\)、\(B\) 各读一次，\(C\) 写一次）的比值。

### 第 2 问
在第 1 问的 kernel 中，若线程按 `threadIdx.x` 对应 \(C\) 的列方向排列：

1. 画出 warp 内 32 个线程对 \(A\) 和 \(B\) 的访问模式示意图。
2. 说明对 \(B\) 的访问为何是 coalesced 的，对 \(A\) 的访问为何不是。
3. 重新设计 thread mapping，使对 \(A\) 和 \(B\) 的 global memory 访问都尽可能 coalesced，写出新的线程索引到 \((i, j)\) 的映射公式。

### 第 3 问
即使完成第 2 问的 coalescing，naive kernel 仍会重复读取 \(A\) 和 \(B\)。

1. 用 \(M, N, K\) 表示每个 \(A[i][k]\) 被读取的次数和每个 \(B[k][j]\) 被读取的次数。
2. 代入数值，计算重复读取导致的额外流量。
3. 说明为什么仅靠 coalescing 无法把实际时间降到由理想流量决定的下界。

### 第 4 问
有人主张：“用了 `__ldg` 和 L2 cache，重复读取不是问题。”

1. 计算 \(A\) 和 \(B\) 的总大小，与 L2 容量（约 40 MB）比较。
2. 分析在 \(K\) 方向迭代时，L2 能否完全容纳工作集。
3. 估算实际 DRAM 流量的下界，并说明与理想流量的差距。

### 第 5 问
引入 \(BM \times BN \times BK\) 的 CTA tile。每个 block 负责 \(C\) 的一个 \(BM \times BN\) 子块，沿 \(K\) 方向迭代，每轮将 \(A\) 的 \(BM \times BK\) tile 和 \(B\) 的 \(BK \times BN\) tile 从 global memory 加载到 shared memory。

1. 写出带 shared memory tiling 的 kernel 框架，包括 `__syncthreads()` 的位置。
2. 用 \(BM, BN, BK\) 表示整个 kernel 的 global memory 总流量。
3. 取 \(BM=BN=64, BK=16\)，计算总流量，并与第 1 问的结果比较。

### 第 6 问
取 \(BM=BN=64, BK=16\)，shared memory 按行主序存储，`__shared__ float As[64][16]`。

1. 说明线程访问 `As[ty][k]` 时为什么会产生 bank conflict。
2. 给出 padding 方案，写出修改后的 shared memory 声明。
3. 证明 padding 后 bank conflict 消除。
4. 说明 padding 对 shared memory 使用量和 occupancy 的影响。

### 第 7 问
在第 5 问的基础上，让每个线程计算 \(TM \times TN\) 个 \(C\) 元素，而不是只算一个。线程从 shared memory 读取 \(A_s\) 的一行和 \(B_s\) 的一列，在寄存器中完成 \(TM \times TN\) 次 FMA。

1. 写出带 register tiling 的内层循环伪代码。
2. 用 \(TM, TN\) 表示每个线程的 shared memory 访问次数与 FMA 次数的比值。
3. 取 \(TM=TN=4\)，计算该比值。
4. 说明为什么增大 \(TM, TN\) 能提高 compute-to-shared-memory ratio，但受限于什么。

### 第 8 问
取 \(BM=128, BN=128, TM=TN=4\)。

1. 计算每个 block 需要的线程数。
2. 计算每个线程用于存储 \(C\) 累加器的寄存器数量。
3. 估算加上 \(A\) fragment、\(B\) fragment、地址和循环变量后的总寄存器数。
4. 判断是否会发生 register spilling，并说明如何权衡 tile 大小与寄存器压力。

### 第 9 问
引入 double buffering：用两块 shared memory buffer 交替加载和计算。

1. 画出时间线，说明无 double buffering 时 global load 与 compute 串行导致的 stall。
2. 写出 double buffering 的伪代码，说明 `__syncthreads()` 的位置如何变化。
3. 取 \(BM=BN=64, BK=16\)，计算单 buffer 和双 buffer 的 shared memory 使用量。
4. 分析在 164 KB 限制下，double buffering 对 occupancy 的影响。

### 第 10 问
在 SIMT FP32 路径上，double buffering 的实测加速可能远低于预期，甚至为负。

1. 说明 double buffering 针对的瓶颈是什么。
2. 如果 profiling 显示 DRAM Throughput 仅 15%、SM Throughput 约 43%，说明实际瓶颈在哪里。
3. 如果 double buffering 导致寄存器数从 168 增至 182、resident blocks 从 3 降至 2，解释为什么性能可能反而下降。
4. 总结：在什么条件下 double buffering 值得引入，在什么条件下不值得。

### 第 11 问
将 global → shared 的加载改为 `float4`（128-bit）。

1. 写出向量化加载的代码片段。
2. 说明对 coalescing 的影响。
3. 计算 instruction count 相对于标量加载的下降比例。
4. 解释为什么在 4096 规模下，float4 向量化往往带来比 double buffering 更明显的收益。

### 第 12 问
讨论 autotuning。

1. 列出至少 5 个可调参数。
2. 说明为什么没有 universally optimal 的 tile 参数。
3. 如果对 4096 规模搜索 48 个合法配置，最优配置是否也适用于 128 规模？为什么？
4. 设计一个简单的 dispatch 策略，根据问题规模选择配置。

---

## 第二部分：Tensor Core 路径

以下各问假设输入为 FP16，累加为 FP32。A100 的 FP16 Tensor Core 峰值约 312 TFLOPS（稠密）。

### 第 13 问
使用 CUDA 的 `nvcuda::wmma` 命名空间。

1. 写出使用 `wmma::load_matrix_sync`、`wmma::mma_sync`、`wmma::store_matrix_sync` 的完整 kernel 框架。
2. 说明 fragment 类型（`matrix_a`、`matrix_b`、`accumulator`）的寄存器布局。
3. 分析 WMMA API 引入的额外数据搬运开销，说明为什么它可能抵消 Tensor Core 的算力优势。

### 第 14 问
绕过 WMMA API，直接用 inline PTX 写 `mma.sync.aligned.m16n8k16.row.col.f32.f16.f16.f32`。

1. 说明该指令的语义：一个 warp 协同完成多大的矩阵乘累加。
2. 写出 A fragment、B fragment、C/D 累加器在每个线程上的寄存器数量与数据类型。
3. 写出调用该指令的 inline PTX 代码片段。
4. 说明相对于 WMMA API，显式 PTX 的优势与代价。

### 第 15 问
用 `ldmatrix.sync.aligned.m8n8.x4.shared.b16` 从 shared memory 加载 A fragment。

1. 说明该指令一次加载多少个矩阵元素，以及每个线程提供什么地址。
2. 说明 B fragment 为什么需要使用 `.trans` 变体，并写出对应的 PTX。
3. 分析 `ldmatrix` 相对于手动 shared memory load 在指令数和 bank conflict 上的优势。
4. 说明 `ldmatrix` 对 shared memory 对齐和布局的要求。

### 第 16 问
用 `cp.async.cg.shared.global [smem], [gmem], 16` 替代同步的 `ld.global` + `st.shared`。

1. 说明 `cp.async` 如何绕过 register file。
2. 比较 `.cg` 和 `.ca` 两种 cache policy 的适用场景。
3. 写出 `cp.async`、`cp.async.commit_group`、`cp.async.wait_group` 的配合方式。
4. 说明在 `cp.async` 路径上，`__syncthreads()` 的位置如何重新设计。

### 第 17 问
将 double buffering 扩展为 \(N\)-stage ring buffer。CUTLASS 在 SM80 上的默认配置是 `kStages=3`。

1. 写出 3-stage 流水线的 prologue、steady state、epilogue 伪代码。
2. 说明 steady state 中 `cp.async.wait_group(N-2)` 的参数为什么是 \(N-2\)。
3. 分析 3-stage 相对于 2-stage 的优势：为什么它能吸收 warp 之间的进度偏差。
4. 取 \(BM=128, BN=128, BK=32\)，计算 3-stage 和 4-stage 的 shared memory 使用量，分析对 occupancy 的影响。

### 第 18 问
`ldmatrix` 对 shared memory 布局有要求。CUTLASS 使用 `Layout_K_SW128_Atom` 做 128 字节宽度的 swizzle。

1. 说明朴素的按行主序存储 FP16 会导致什么问题。
2. 描述 swizzle 的基本思想：如何通过异或置换使 `ldmatrix` 的地址散布在 32 个 bank 上。
3. 比较 swizzle 与传统 padding 方案在 shared memory 开销上的差异。
4. 说明为什么 CUTLASS 在 Tensor Core 路径上首选 swizzle 而非 padding。

### 第 19 问
在 mma 的 inner loop 中，A 和 B fragment 也需要被复用于多个 mma 指令。

1. 说明如果每轮 K 迭代都重新从 shared memory 加载 fragment，`ldmatrix` 吞吐可能成为瓶颈的原因。
2. 设计一个 fragment reuse 方案：一个 warp 有 \(4 \times 8\) 个 mma 指令，A fragment 加载几次，B fragment 加载几次？
3. 计算该方案下 A fragment、B fragment、累加器各自需要的寄存器数量。
4. 分析 register pressure 与 fragment reuse 之间的权衡。

### 第 20 问
量化路径：将输入从 FP16 降为 INT8 或 INT4。

1. 写出 INT8 的 `mma.sync.aligned.m16n8k32.row.col.s32.s8.s8.s32` 的指令语义。
2. 说明 INT8 相对于 FP16 在内存带宽上的节省比例。
3. 说明 INT4 的 mma 指令 K 维度是多少，以及相对于 INT8 的进一步节省。
4. 分析量化路径下，K 维度变化对每轮 shared memory 加载量和指令数的影响。

---

## 第三部分：综合

### 第 21 问
给定以下 profiling 数据（假设已实现第 13–19 问的所有优化）：

| 指标 | 数值 |
|------|------|
| DRAM Throughput | 15% |
| SM Throughput | 43% |
| L1 Wavefronts Shared Excessive | 0 |
| Register Spills | 0 |
| Achieved Occupancy | 25% |
| Warp Stall Long Scoreboard | 高 |
| Warp Stall Short Scoreboard | 低 |

1. 判断该 kernel 的瓶颈类型。
2. 给出三种可能的改进方向，并说明如何用 profiling 区分它们。
3. 如果实测 15 TFLOPS（FP32 路径），计算相对于峰值的百分比，并说明落在 roofline 的什么位置。

### 第 22 问
从 naive kernel 到最终 kernel，列出每一步优化带来的性能提升倍数。

1. 画出累积增益图（横轴为优化步骤，纵轴为 TFLOPS）。
2. 指出哪一步收益最大，哪一步收益最小。
3. 解释收益最大的一步为什么关键，收益最小的一步是否应该跳过。

### 第 23 问
讨论极端形状。

1. 若 \(M=64, N=4096, K=4096\)（瘦长矩阵），上述 tiling 策略需要如何调整？说明 \(BM, BN, BK\) 的选择依据。
2. 若 \(M=N=K=128\)（小矩阵），tiling 还有意义吗？为什么？
3. 若 \(M=N=K=8192\)，shared memory 和 register 的约束会发生什么变化？

### 第 24 问
有人声称：“只要 tile size 足够大，GEMM 就是 compute-bound，不需要考虑 memory。”

1. 用 roofline 分析反驳：tile size 增大如何改变 arithmetic intensity？它的上界是什么？
2. 用 occupancy 分析反驳：tile size 增大对 shared memory 和 register 的压力如何影响 resident blocks？
3. 用 shared memory 容量反驳：\(BM=BN=256, BK=32\) 时，FP16 数据需要多少 shared memory？能否驻留多个 block？
4. 用 register 压力反驳：累加器寄存器数量如何随 tile size 增长？何时会发生 spilling？
5. 给出修正后的正确说法。

### 第 25 问
工业级参考实现参数。

1. 列出 CUTLASS SM80 默认 GEMM 配置的关键参数（CTA Tile、Warp Tile、mma 指令、Stages、Shared Layout）。
2. 说明这些参数是如何从第 5–19 问的约束中推导出来的。
3. 如果要在 H100（SM90）上实现同等功能的 GEMM，哪些参数需要改变？为什么？
4. 说明 WGMMA 与 mma.sync 在指令语义上的根本差异。

---

**附：可用的常量**

- A100 SM 数量：108
- A100 每 SM shared memory：164 KB
- A100 每 SM 寄存器文件：256 KB
- A100 FP32 峰值：19.5 TFLOPS
- A100 FP16 Tensor Core 峰值：312 TFLOPS
- A100 HBM 带宽：1.5 TB/s
- L2 容量：40 MB
- 4096² 矩阵 FP32 大小：64 MB
- 4096² 矩阵 FP16 大小：32 MB
- 4096³ 总 FLOPs：\(1.37 \times 10^{11}\)