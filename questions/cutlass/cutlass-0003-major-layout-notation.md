---
id: cutlass-0003
type: blank
topic: cutlass-cute-layout
chapter: CuTe 代数
tags: [CuTe, layout, column-major, row-major]
layer: 识记
wing: 基础
difficulty: 2
caseSensitive: false
source: "CUTLASS media/docs/cutlass_compiler/cute_concepts/01_cute_types.rst §Bijective"
---

CuTe 用 `(shape):(stride)` 的记法描述一个 layout：括号里是各模式的**长度**，
冒号后面是对应的**步长**。步长为 1 的那个模式意味着「沿着它走一步只前进 1 个元素」，
也就是变化最快的那个方向。

对于一个 $M \times N$ 的矩阵，写出它的两种标准 layout 在 CuTe 里的记法
（用 $M$、$N$ 表示维度，字面写出来即可）：

**第 1 空**：列主序（column-major），即 $M$ 方向变化最快。
**第 2 空**：行主序（row-major），即 $N$ 方向变化最快。

## 答案
(M,N):(1,M)|~^\(\s*M\s*,\s*N\s*\)\s*:\s*\(\s*1\s*,\s*M\s*\)$
(M,N):(N,1)|~^\(\s*M\s*,\s*N\s*\)\s*:\s*\(\s*N\s*,\s*1\s*\)$

## 解析
两种记法只差步长那一对数字，关键是**先读 shape，再读 stride，然后把 stride 与 shape 一一对应**：

| | 记法 | shape 第 1 项 | 对应的 stride |
|---|---|---|---|
| 列主序 | `(M,N):(1,M)` | $M$ | $1$ ← $M$ 方向每步 1 个元素 |
| 行主序 | `(M,N):(N,1)` | $M$ | $N$ ← $M$ 方向要跨过一整行 |

以行主序 `(M,N):(N,1)` 为例：坐标 $(i,j)$ 的线性索引是 $iN + j$，
即第 $i$ 行整体排在第 $j$ 列之前——这正是「行优先存储」的定义。
列主序 `(M,N):(1,M)` 给出 $i + jM$，把列排在前面。

两个都容易记反，一个稳定的自查办法是**代一个具体尺寸**：取 $M=2, N=3$。

- 行主序 `(2,3):(3,1)`：索引是 $3i + j$。逐行读出来是 $0,1,2,\ 3,4,5$。
- 列主序 `(2,3):(1,2)`：索引是 $i + 2j$。逐行读出来是 $0,2,4,\ 1,3,5$。

同一个矩阵、同一次逐行读取，元素在内存里的落点完全不同——这就是 stride 那一对数字
在决定的事。两组都恰好命中 $[0,6)$ 且无重复，所以两种 layout 都是 bijective 的。

> 这两种 layout 都是 **bijective** 的：无重复、无空洞，
> 因此 `cute.left_inverse` 与 `cute.right_inverse` 都成立。
