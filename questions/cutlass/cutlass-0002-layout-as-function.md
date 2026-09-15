---
id: cutlass-0002
type: single
topic: cutlass-cute-property
chapter: CuTe 代数
tags: [CuTe, layout, injective, surjective, bijective, complement]
layer: 理解
wing: 综合
difficulty: 3
source: "CUTLASS media/docs/cutlass_compiler/cute_concepts/01_cute_types.rst §Layouts as Functions"
---

在 CuTe 里，一个 layout **本身就是一个函数**：把坐标映射成线性索引。
于是函数论里的那些性质——injective、surjective、bijective——对 layout 同样成立，
CuTe 的若干算子正是以它们作为前置或后置条件。

关于这些性质，下列哪一项说法**正确**？

## 选项
- A. 只要 layout 含有一个 stride 为 0 的模式，它就不是 injective 的
- B. 一个 layout 只要满足 injective，就一定同时满足 bijective
- C. 对任意 layout `L` 与目标范围 `[0, M)`，`(L, complement(L, M))` 一定完整覆盖 `[0, M)`
- D. `(2,2):(4,1)` 的像是 `[0, 6)` 这个连续区间

## 答案
A

## 解析
### 选项辨析
- **B 错**：把「必要」当成了「充分」。injective 只是 `cute.left_inverse` 的前置条件；
  bijective 还额外要求 surjective onto `[0, size(L))`。列主序 `(M,N):(1,M)` 与
  行主序 `(M,N):(N,1)` 恰好两条都满足，所以这个说法在常见 layout 上看不出问题——
  这正是它的迷惑之处。
- **C 错**：过度外推。`complement` 只保证 `(L, L*)` 比 `L` 单独**覆盖得更多**；
  文档明确写了它**不保证**完全覆盖，具体能覆盖多少取决于 `L` 的 stride 结构。
- **D 错**：`(2,2):(4,1)` 的坐标只有 $(i,j)\in\{0,1\}^2$，索引 $4i+j\in\{0,1,4,5\}$，
  其中 `{2,3}` 被跳过——像**不是**连续区间。这正是「不 surjective」的样子：
  它只走到码域的一个子集。

### 三条性质
layout 作为函数，三条性质是层层加码的：

| 性质 | 定义 | 在 CuTe 里的用途 |
|---|---|---|
| **injective** | 不同坐标映射到不同索引，没有两个坐标撞在一起 | `cute.left_inverse` 的**前置条件**：不 injective 就无法从索引唯一还原坐标 |
| **surjective onto `[0,M)`** | `[0,M)` 里每个索引都被至少一个坐标命中 | `cute.right_inverse` 的后置条件 |
| **bijective onto `[0,size(L))`** | 既是 injective 又是 surjective：无空洞、无重复 | 最良性的一类，逆运算两个方向都成立 |

**最直接的构造非 injective layout 的办法，就是塞一个 stride 为 0 的模式**：
该模式对索引没有任何贡献，于是沿着这根轴的所有坐标都落到同一个索引上。
例如 `(4,8):(1,0)` 里，$L(i,0)=L(i,1)=\dots=L(i,7)$——整根第二轴被压缩成一个点。

而 surjectivity 的失败更隐蔽：它看的是**码域有没有空洞**。
`(2,2):(4,1)` 只命中 `{0,1,4,5}`，跳过了 `{2,3}`，所以它不 surjective onto 自己的 cosize。
`complement` 的作用就是把「漏掉的那些位置」整理成另一个 layout，
让 `(L, L*)` 这一对覆盖得更多——但仅此而已，不承诺补满。

> 注意：这两个性质是**独立**的。既存在 injective 但非 surjective 的 layout，
> 也存在 surjective 但非 injective 的（后者一定含 stride-0 模式）。
