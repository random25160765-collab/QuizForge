---
id: cuda-0003
type: problem
topic: cuda-gemm-tiling
chapter: GEMM 优化
tags: [GEMM, roofline, tiling, shared memory, 综合大题]
difficulty: 5
source: "参考 fashidati.md 的大题组织方式；数据取自 A100 公开规格"
---

**题干** 设 $A \in \mathbb{R}^{M \times K}$、$B \in \mathbb{R}^{K \times N}$、$C \in \mathbb{R}^{M \times N}$，
计算 $C = A \times B$。除特别说明外取 $M = N = K = 4096$，输入 FP32，累加 FP32。
目标硬件为 NVIDIA A100（SM80）：FP32 峰值约 $19.5$ TFLOPS，
FP16 Tensor Core 峰值约 $312$ TFLOPS，HBM 带宽约 $1.5$ TB/s，
每 SM shared memory 上限 $164$ KB，每 SM 寄存器文件 $256$ KB，L2 约 $40$ MB。
只允许使用 CUDA C++ 与 inline PTX。

**说明** 本大题分三部分。每一问的结论会被后续小问复用，建议按顺序作答。
部分小问需要定量计算或写出可编译的伪代码。

**常用常量**

| 项目 | 数值 |
|---|---|
| $4096^2$ 矩阵 FP32 大小 | $64$ MB |
| $4096^2$ 矩阵 FP16 大小 | $32$ MB |
| 理想访存量（读 A、读 B、写 C） | $3 \times 64\ \text{MB} = 192\ \text{MB}$ |

## 小问

### 第一部分 · 第 1 问｜naive kernel 的访存量

写出 naive kernel：每个线程计算 $C$ 的一个元素，
每次 FMA 都直接从 global memory 读取 $A$ 与 $B$。

1. 用 $M, N, K$ 表示整个 kernel 的 global memory 读取总次数。
2. 代入数值，计算实际访存量与理想访存量的比值。

#### 提示
$C$ 的一个元素需要 $K$ 次乘累加，每次用到一个 $A$ 的元素和一个 $B$ 的元素。

#### 参考答案
每个输出元素 $C[i][j]$ 需要 $K$ 次 FMA，每次从 global memory 读一个 $A[i][k]$ 与一个 $B[k][j]$，
因此每个输出元素的读取次数是 $2K$。共有 $M \times N$ 个输出元素：

$$
\text{读取次数} = 2MNK
$$

代入 $M=N=K=4096$：$2 \times 4096^3 \approx 1.37 \times 10^{11}$ 次读取，
每次 4 字节，即

$$
2MNK \times 4 = 1.37 \times 10^{11} \times 4 \approx 550\ \text{GB}
$$

而理想访存量是 $192$ MB，比值约为

$$
\frac{550\ \text{GB}}{192\ \text{MB}} \approx 2860
$$

也就是说 naive kernel 的访存量是理想值的近三千倍。

#### 评分要点
- 正确给出读取次数 $2MNK$（不是 $MNK$，也不是 $MN^2K$）
- 明确说明系数 2 来自「每个 FMA 需要读 A 与 B 各一个元素」
- 算出实际访存量约 550 GB（允许 500–600 GB 量级的说法）
- 与理想值 192 MB 相除，给出约 $2.9 \times 10^3$ 的比值
- 指出该比值与 $K$ 同阶，并解释原因是 A、B 被重复读取 $N$ 次 / $M$ 次

### 第一部分 · 第 2 问｜coalescing

设线程按 `threadIdx.x` 对应 $C$ 的列方向（$j$ 方向）排列。

1. 说明 warp 内 32 个线程对 $B[k][j]$ 的访问是否 coalesced。
2. 说明对 $A[i][k]$ 的访问是否是 coalesced。
3. 给出使两者都尽可能 coalesced 的线程索引到 $(i, j)$ 的映射公式。

#### 参考答案
对固定的 $k$，32 个线程访问 $B[k][j], B[k][j+1], \dots, B[k][j+31]$。
按行主序 $B[k][j]$ 的地址是 $k \cdot N + j$，相邻线程地址相差 4 字节 ——
**落在同一个 128 字节 cache line 上，是 coalesced 的**。

而 $A[i][k]$ 中 $i$ 随线程变化、$k$ 固定，地址是 $i \cdot K + k$，
相邻线程地址相差 $4K = 16384$ 字节 ——
**每个线程各自命中不同的 cache line，完全没有 coalescing**。

标准做法是让线程在 $M$ 方向连续：

```cuda
int row = blockIdx.y * blockDim.y + threadIdx.y;   // i
int col = blockIdx.x * blockDim.x + threadIdx.x;   // j
// 取 blockDim.x = 32 时，warp 内 32 个线程沿 j 连续
```

这样对 $B$ 的访问依然是 coalesced，而对 $A$ 的访问变成
「同一行内 32 个线程读 $A[i][k]$ 的**同一个地址**」，
硬件会做 broadcast，只产生一次访存事务。

#### 评分要点
- 明确判断对 B 的访问是 coalesced，并给出地址连续的推导
- 明确判断对 A 的访问不是 coalesced，并指出 stride 为 4K 字节
- 给出让 threadIdx.x 对应 j（列方向）、threadIdx.y 对应 i 的映射
- 指出 $A$ 的访问会退化为同一地址的广播（broadcast），只需一次事务
- 说明 coalescing 只减少事务数，不减少数据总量，因此不能单独解决重复读取问题

### 第二部分 · 第 3 问｜shared memory tiling

引入 $BM \times BN \times BK$ 的 CTA tile：每个 block 计算 $C$ 的一个
$BM \times BN$ 子块，沿 $K$ 方向迭代，每轮把 $A$ 的 $BM \times BK$ tile 与
$B$ 的 $BK \times BN$ tile 从 global memory 载入 shared memory。

取 $BM = BN = 64$、$BK = 16$。

1. 用 $BM, BN, BK$ 表示整个 kernel 的 global memory 总访存量。
2. 代入数值并计算相对 naive 版本的减少倍数。
3. 计算这一配置下单 buffer 的 shared memory 占用。

#### 参考答案
每个 CTA 覆盖一个 $BM \times BN$ 的输出块，沿 $K$ 方向共有 $K / BK$ 轮；
每轮载入 $A$ 的 $BM \times BK$ 与 $B$ 的 $BK \times BN$，因此每个 CTA 的访存量是

$$
\frac{K}{BK}(BM \cdot BK + BK \cdot BN) = K(BM + BN)
$$

共有 $\frac{M}{BM} \cdot \frac{N}{BN}$ 个 CTA，于是总访存量

$$
\text{Traffic} = \frac{MN}{BM \cdot BN} \cdot K (BM + BN)
= MNK\left(\frac{1}{BN} + \frac{1}{BM}\right)
$$

代入 $BM = BN = 64$：$MNK \times \frac{2}{64} = \frac{MNK}{32}$。
相对 naive 的 $2MNK$，减少了

$$
\frac{2MNK}{MNK/32} = 64
$$

即访存量降为原来的 $1/64$。单 buffer 的 shared memory 占用：

$$
(BM \cdot BK + BK \cdot BN) \times 4\ \text{B}
= (64 \times 16 + 16 \times 64) \times 4 = 8192\ \text{B} = 8\ \text{KB}
$$

#### 评分要点
- 给出每个 CTA 的访存量 $K(BM + BN)$，并进一步推出总访存量 $MNK(1/BN + 1/BM)$
- 代入得到 $MNK/32$
- 与 naive 的 $2MNK$ 相除，得到约 64 倍的减少
- 算出单 buffer shared memory 占用 8 KB
- 可选加分：指出访存量只与 tile 的周长 $BM + BN$ 有关，因此加大 tile 能提高复用率
- 可选加分：指出 8 KB 远小于 164 KB，所以还有余量做多级缓冲

### 第二部分 · 第 4 问｜bank conflict 与 padding

shared memory 按行主序存储，声明为 `__shared__ float As[64][16]`。

1. 说明线程读取 `As[ty][k]` 时是否会产生 bank conflict。
2. 若存在冲突，给出 padding 方案并写出修改后的声明。
3. 说明 padding 对 shared memory 占用与 occupancy 的影响。

#### 参考答案
`As[ty][k]` 的地址是 $ty \times 16 + k$（单位：4 字节字），
bank 编号为 $(16\,ty + k) \bmod 32$。

对固定的 $k$，一个 warp 的 32 个线程（$ty = 0 \dots 31$）访问的 bank 是
$(16\,ty + k) \bmod 32$。因为 $\gcd(16, 32) = 16$，
$16\,ty \bmod 32$ 只在 $\{0, 16\}$ 两个值之间取值，
**32 个线程只落在 2 个 bank 上，形成 16-way bank conflict**。

padding 方案是把行宽从 16 改成 17：

```cuda
__shared__ float As[64][17];   // 每行多一个 float
```

此时地址是 $17\,ty + k$，$\gcd(17, 32) = 1$，
$(17\,ty + k) \bmod 32$ 随 $ty$ 遍历全部 32 个 bank，冲突消除。

代价：占用从 $64 \times 16 \times 4 = 4096$ B 增到 $64 \times 17 \times 4 = 4352$ B，
每行多 4 字节，增幅约 6%。这个增幅通常不影响 occupancy
（尤其当 shared memory 上限远未用满时），因此是划算的。

#### 评分要点
- 推出 bank 编号表达式 $(16\,ty + k) \bmod 32$
- 指出 $\gcd(16,32)=16$，32 个线程只落在 2 个 bank，形成 16-way conflict
- 给出 `As[64][17]` 的 padding 方案（[16] → [17]）
- 解释 padding 生效的原因是行宽与 bank 数互素
- 量化占用变化（4096 B → 4352 B）
- 说明代价与收益的权衡，并指出可能压低 occupancy 的场景

### 第三部分 · 第 5 问｜register tiling 与算术强度

在 CTA tile 基础上，让每个线程计算 $TM \times TN$ 个 $C$ 元素：
线程从 shared memory 读取 $A_s$ 的一行和 $B_s$ 的一列，
在寄存器中完成 $TM \times TN$ 次 FMA。取 $TM = TN = 4$。

1. 用 $TM, TN$ 表示每个线程的 FMA 次数与 shared memory 访问次数的比值。
2. 代入 $TM = TN = 4$ 计算该比值。
3. 说明为什么增大 $TM, TN$ 能提高该比值，以及它受什么限制。

#### 参考答案
每个线程每轮需要读 $TM$ 个 $A_s$ 元素与 $TN$ 个 $B_s$ 元素，
合计 $TM + TN$ 次 shared memory 访问，而 FMA 次数是 $TM \times TN$，因此

$$
\text{ratio} = \frac{TM \cdot TN}{TM + TN}
$$

代入 $TM = TN = 4$：$\frac{16}{8} = 2$，即每次 shared memory 访问支撑 2 次 FMA。
若 $TM = TN = 8$，比值升到 $\frac{64}{16} = 4$ —— 复用率随 tile 边长线性增长。

限制来自**寄存器容量**：每个线程至少要 $TM \times TN$ 个累加器寄存器。
$TM = TN = 8$ 时累加器就要 64 个，加上 A/B fragment、地址与循环变量后
很容易超过每线程 255 个寄存器的上限，触发 spilling；
同时寄存器用量上升会减少常驻 block 数，压低 occupancy。
因此 $TM, TN$ 的选择是在「提高 shared memory 复用率」与
「控制寄存器压力 / 保持 occupancy」之间的权衡。

#### 评分要点
- 给出比值表达式 $TM \cdot TN / (TM + TN)$
- 代入 $TM=TN=4$ 得到 2
- 指出累加器寄存器数量为 $TM \times TN$，是主要约束
- 说明寄存器压力过大会造成 spilling 并降低 occupancy
- 给出「在寄存器预算内尽量放大 tile」的结论
- 可选加分：指出该比值的极大化条件以及 $TM=TN$ 时比值随边长线性增长

### 第三部分 · 第 6 问｜瓶颈判断

给定 profiling 数据（已实现前三部分的全部优化）：

| 指标 | 数值 |
|------|------|
| DRAM Throughput | 15% |
| SM Throughput | 43% |
| Achieved Occupancy | 25% |
| Register Spills | 0 |
| Warp Stall Long Scoreboard | 高 |

1. 判断该 kernel 属于哪一类瓶颈。
2. 给出两种可能的改进方向，并说明如何用 profiling 区分它们。
3. 若实测 $15$ TFLOPS（FP32 路径），计算相对峰值的比例并说明落在 roofline 的什么位置。

#### 参考答案
**1. 瓶颈判断。** DRAM 只有 15% 而 SM 已到 43%，
且 Long Scoreboard 停顿高（表示 warp 在等待访存返回），
说明 kernel **既不是带宽受限，也没有打满算力**，而是**延迟受限**：
occupancy 只有 25%，可用 warp 数不足以掩盖访存与流水线延迟。
换句话说，瓶颈在「并发度不足导致的延迟暴露」，而不是任何一条带宽/算力上限。

**2. 两个方向。**

*方向一：提高 occupancy。* 减少每线程寄存器用量（缩小 $TM, TN$ 或减少
fragment 复用）、缩小 shared memory 占用（降低 stage 数）。
用 「Occupancy / Registers Per Thread / Shared Memory Per Block」这几个
指标即可确认改动是否生效；如果 occupancy 上去了而吞吐不涨，说明瓶颈在别处。

*方向二：减少访存延迟本身。* 把 global → shared 的加载改成
`cp.async`（绕过寄存器）、用 128 位 `float4` 加载、提高 stage 数做更深流水。
区分手段是看 `L1 Wavefronts`、`DRAM Throughput` 与 `Long Scoreboard`
是否同时下降；如果 Long Scoreboard 仍是主要停顿，说明延迟没被掩盖住。

**3. 相对峰值。** $\frac{15}{19.5} \approx 77\%$。

在 roofline 上先算转折点算术强度：

$$
I_{\text{ridge}} = \frac{19.5\ \text{TFLOP/s}}{1.5\ \text{TB/s}} = 13\ \text{FLOP/byte}
$$

分块后每个 CTA 的算术强度为

$$
\frac{2 \cdot BM \cdot BN \cdot BK}{4(BM \cdot BK + BK \cdot BN)}
= \frac{2 \cdot BM \cdot BN}{4(BM + BN)}
$$

代入 $BM = BN = 64$ 得 $8192 / 512 = 16\ \text{FLOP/byte}$，
已经**越过转折点**，因此理论位置在算力平台上沿。
但实测只有 77% 峰值，说明没有真正触到平台 —— 差距来自 occupancy 不足
导致的延迟暴露，而不是带宽或算力不足。

#### 评分要点
- 判断为延迟受限（latency-bound），而不是带宽受限或算力受限
- 用 DRAM 15% 与 SM 43% 两个数字佐证「两条上限都没到」
- 明确指出 Long Scoreboard 高 + occupancy 低 = 延迟未被掩盖
- 给出至少两个改进方向（提高 occupancy / 减少并掩盖访存延迟）
- 说明用哪些 profiling 指标区分这两个方向
- 算出 $15/19.5 \approx 77\%$
- 算出转折点算术强度约 13 FLOP/byte，并说明分块后约 16 已越过转折点
- 指出理论应在算力平台，实测差距来自延迟而非上限
