---
id: tt-arch-0001
type: multi
topic: tt-tensix-riscv
chapter: Tensix Tile 结构
tags: [Tensix, Baby RISCV, L1, NoC]
difficulty: 3
source: "tt-isa-documentation / BlackholeA0/TensixTile/README.md"
---

一个 Tensix tile 里包含若干固定的硬件组件。下列说法正确的是哪些？

## 选项
- A. 每个 Tensix tile 有 1536 KiB 的 L1 RAM，指令只能从 L1 取指与执行，不能从其它内存区域取指
- B. 每个 Tensix tile 有 5 个「baby」RISC-V 核，分别是 RISCV B、T0、T1、T2、NC；它们是 32 位顺序单发射核，设计目标是面积与功耗效率
- C. Tensix 协处理器内部包含 2 个 Unpacker、1 个 Matrix Unit（FPU）、1 个 Vector Unit（SFPU）、1 个 Scalar Unit（ThCon）和 4 个 Packer
- D. 5 个 RISC-V 核自身就是主要算力来源，高性能 kernel 的关键是让它们尽可能多地执行标量运算
- E. 每个 tile 有 2 个 NoC 连接，以及 1 个 NoC overlay 辅助处理 NoC 事务

## 答案
A,B,C,E

## 解析
数据来自 `tt-isa-documentation` 的 `BlackholeA0/TensixTile/README.md` 与 `BabyRISCV/README.md`。

**A 正确。** 文档原文：「Each Tensix tile contains 1536 KiB of RAM called L1」，
并且明确说明「Instructions can only be fetched and executed from L1;
instructions cannot be executed out of any other memory regions.」
每个 RISC-V 核面前还挂着一个小的 **L0 指令缓存**，负责从 L1 取指。

**B 正确。** 文档原文：「Each Tensix tile contains five RISCV cores ... they are relatively
small 32-bit in-order single-issue cores, optimized for area and power efficiency rather than
for high performance.」五个核的名字是 RISCV **B**、**T0**、**T1**、**T2**、**NC**。
指令集为 RV32IM，外加 Zicsr / Zaamo / Zba / Zbb，以及部分的 Zicntr / F / Zfh；
**T2 额外实现了部分 V（向量）扩展**——它的寄存器堆多了 32 个 128 位向量寄存器，
读端口也从 3×32b 扩到 4×128b。

**C 正确。** 这是 Tensix 协处理器的完整组成，逐个对应：

| 组件 | 数量 | 作用 |
|---|---|---|
| Unpacker | 2 | 把数据从 L1 搬进协处理器 |
| Matrix Unit（FPU） | 1 | 低精度矩阵乘累加 |
| Vector Unit（SFPU） | 1 | 32 宽 SIMD，含 FP32 乘累加 |
| Scalar Unit（ThCon） | 1 | 整数标量运算与对 L1 的 128 位访存（含原子操作） |
| Packer | 4 | 把结果从协处理器写回 L1 |

**D 错误。** 文档明确写着：「The RISCV cores are not intended to achieve high performance
on their own; they are intended to **oversee the other components that actually drive
performance**.」它们每个周期最多执行一条指令、主频 1.35 GHz，真正的算力来自
Matrix Unit / Vector Unit 与 NoC。文档给出的一个自然分工是
**两个核管 NoC、三个核管 Tensix 协处理器**。

**E 正确。** 每个 tile 有 2 个 NoC 连接与 1 个 NoC overlay（一个帮助处理 NoC 事务的小协处理器）。

一个容易踩的坑：`.ttinsn` 是 Tensix 自己独有的指令集扩展。在 RISCV T$i$ 中，
L0 指令缓存可以把最多 4 条相邻的 `.ttinsn` 融合成一条 64/96/128 位指令，
让 RISC-V 核在一个周期里把它交给 Tensix 前端；但 Tensix 前端的**出队速率上限是
每周期 1 条** Tensix 指令——入队 4 条、出队 1 条，这个不对称是理解 Tensix
编程模型（把大量工作下推给协处理器指令流）的关键。
