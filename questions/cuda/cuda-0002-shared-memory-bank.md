---
id: cuda-0002
type: blank
topic: cuda-mem-shared
chapter: 内存层次与共享内存
tags: [shared memory, bank conflict, padding]
difficulty: 4
source: "CUDA C++ Programming Guide §9.2.3.3 Shared Memory Bank Conflicts"
---

在 32 位模式下，NVIDIA GPU 的 shared memory 被划分为 32 个 bank，
每个 bank 宽 4 字节，第 $i$ 个 4 字节字的地址映射到 bank $i \bmod 32$。

考虑按列访问一个 $N \times N$ 的二维数组：

```cuda
#define N 32
__global__ void colAccess(float tile[N][N]) {
    int tid = threadIdx.x;          // tid = 0 .. 31，恰好一个 warp
    float acc = 0.f;
    #pragma unroll
    for (int c = 0; c < N; ++c) {
        acc += tile[tid][c];        // 每个线程负责一整行
    }
}
```

**第 1 空**：某一轮迭代中，32 个线程同时读取 `tile[tid][c]`，
这些访问会集中落到多少个 bank 上？（填一个数字）

**第 2 空**：把数组声明改成 `float tile[N][N + k]` 可以消除冲突，`k` 至少取多少？
（填一个数字）

## 答案
1
1

## 解析
**第 1 空：1 个 bank（32-way bank conflict）。**

按行主序，`float tile[32][32]` 中元素 `tile[r][c]` 的地址是 $r \times 32 + c$（单位：4 字节字）。
第 $tid$ 个线程访问 `tile[tid][c]`，地址为

$$
tid \times 32 + c
$$

bank 编号为

$$
(tid \times 32 + c) \bmod 32 = c
$$

因为 $\gcd(32, 32) = 32$，$tid \times 32$ 恒为 32 的倍数，
**32 个线程全部落在同一个 bank（编号为 $c$ 的那个）上，且访问的是不同的字**，
因此形成一个 **32-way bank conflict**，硬件必须串行处理 32 次，
这一轮的访存吞吐只有理想情况的 $1/32$。

注意与「无冲突」的情况区分：如果改成按行访问 `tile[c][tid]`，
地址是 $c \times 32 + tid$，bank 为 $(c \times 32 + tid) \bmod 32 = tid$，
32 个线程各占一个 bank，完全没有冲突。**同一个数组，访问方向不同，性能差 32 倍**。

**第 2 空：$k = 1$。**

把行宽改成 $N + k$ 后，元素 `tile[r][c]` 的地址变成 $r(N+k) + c$。
取 $k = 1$，地址为 $r \times 33 + c$，bank 为

$$
(33r + c) \bmod 32 = (r + c) \bmod 32
$$

对于固定的 $c$，$r = tid = 0 \dots 31$ 时 $(tid + c) \bmod 32$ 恰好遍历 0..31，
**每个 bank 正好一个线程，冲突消除**。关键条件是 $\gcd(33, 32) = 1$，
即 stride 与 bank 数量互素。

这就是所谓的 **padding 技巧**。代价是每行多占 4 字节：

$$
\text{占用} = 4 \times 32 \times 33 = 4224 \text{ B} \quad \text{vs} \quad 4 \times 32 \times 32 = 4096 \text{ B}
$$

在 CUDA 里还可以用动态共享内存 + 手动索引达到同样效果：

```cuda
extern __shared__ float smem[];
float* row = &smem[r * (N + 1)];   // 显式 +1 填充
```

工程上常见的两个坑：

* padding 会增加 shared memory 占用，可能压低 occupancy —— 需要在「消除 bank conflict」
  与「保持足够多的常驻 block」之间权衡。
* `__shared__` 的声明顺序会影响 bank 映射，把高频同时访问的数组错开摆放是另一类优化手段。
