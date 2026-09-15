---
id: ptx-0001
type: blank
topic: ptx-state-space
chapter: 状态空间与指令语义
tags: [PTX, state space, ld/st, 内存模型]
difficulty: 3
caseSensitive: false
source: "PTX ISA — Chapter 5 State Spaces, Types, and Variables"
---

阅读下面这段 PTX，回答三个问题。

```ptx
.version 8.0
.target sm_90
.address_size 64

.visible .entry vec_add(
    .param .u64 vec_add_param_0,
    .param .u64 vec_add_param_1,
    .param .u64 vec_add_param_2,
    .param .u32 vec_add_param_3
)
{
    .reg .pred  %p<2>;
    .reg .b32   %r<8>;
    .reg .b64   %rd<8>;

    ld.param.u64    %rd1, [vec_add_param_0];
    ld.param.u64    %rd2, [vec_add_param_1];
    ld.param.u64    %rd3, [vec_add_param_2];
    ld.param.u32    %r1,  [vec_add_param_3];

    mov.u32         %r2, %ctaid.x;
    mov.u32         %r3, %ntid.x;
    mov.u32         %r4, %tid.x;
    mad.lo.s32      %r5, %r2, %r3, %r4;

    setp.ge.s32     %p1, %r5, %r1;
    @%p1 bra        $L__BB0_2;

    mul.wide.s32    %rd4, %r5, 4;
    add.s64         %rd5, %rd1, %rd4;
    add.s64         %rd6, %rd2, %rd4;
    add.s64         %rd7, %rd3, %rd4;

    ld.global.f32   %f1, [%rd5];
    ld.global.f32   %f2, [%rd6];
    add.f32         %f3, %f1, %f2;
    st.global.f32   [%rd7], %f3;

$L__BB0_2:
    ret;
}
```

**第 1 空**：`%tid.x` 属于哪一类状态空间？填一个 PTX 中的状态空间关键字（不含点号）。

**第 2 空**：`ld.global.f32 %f1, [%rd5];` 中，方括号里的 `%rd5` 默认使用哪个状态空间
来解析地址？填一个关键字（不含点号）。

**第 3 空**：要把上面这条 `ld.global` 改成**不经过 L1 缓存、直接从 L2 读取**，
应该在指令上追加哪个 cache operator？（填形如 `.cg` 这样的操作符）

## 答案
special
generic
.cg|cg

## 解析
**第 1 空：`special`。**

PTX 一共定义了 8 个状态空间，可以分成三类：

| 类别 | 状态空间 | 说明 |
|---|---|---|
| 用户可见寄存器 | `.reg` | 每个线程私有的通用寄存器 |
| 用户可见内存 | `.global` `.shared` `.local` `.const` `.param` | 有地址、可以 `ld`/`st` |
| 特殊寄存器 | `.sreg` | 只读的硬件信息，如 `%tid`、`%ctaid`、`%laneid`、`%clock` |

`%tid.x`、`%ntid.x`、`%ctaid.x`、`%clock`、`%lanemask_lt` 这些都是特殊寄存器，
用 `.reg` 声明不了，也不需要声明。

**第 2 空：`generic`。**

`ld.global` 里虽然写了 `.global`，但 PTX 的地址解析规则是：
**不带状态空间前缀的地址（即 `[%rd5]` 这种裸地址）按「泛型地址（generic）」处理**，
运行时由硬件根据地址落在哪一段来转发到对应的物理空间。
这带来一个重要的性能含义：泛型地址访问需要额外的地址翻译判断，
而显式写出 `.global`/`.shared` 作为**状态空间限定**可以省掉这一步。

也就是说上面那条指令实际上可以拆成两个语义层：

```ptx
ld.global.f32  %f1, [%rd5];    ; 限定：目标是 global memory
                               ; 地址：generic（未限定），需要运行时判定
```

对比之下 `ld.shared.f32 %f1, [%r_addr];` 的地址本身就是 shared 空间下的偏移，
不经过泛型翻译。这也是为什么 `cvta.to.global` / `cvta.to.shared` 这类
「地址空间转换」指令在编译器输出里很常见。

**第 3 空：`.cg`（cache at global level）。**

PTX 的 cache operator 作用在 `ld` / `st` 的第二级（缓存级别）上：

| 操作符 | 含义 |
|---|---|
| `.ca` | cache at all levels（默认，L1 + L2 都缓存） |
| `.cg` | cache globally —— **绕过 L1**，只在 L2 缓存 |
| `.cs` | cache streaming —— 标记为流式数据，倾向于被优先驱逐 |
| `.lu` | last use |
| `.cv` | 不缓存，每次重新读取（volatile 语义） |

因此答案是追加 `.cg`：

```ptx
ld.global.cg.f32  %f1, [%rd5];
```

**实际意义**：如果这份数据只被读取一次（比如 KV cache 的一次性扫描、
流式算子里的输入张量），把它塞进 L1 会污染本来更值得缓存的复用数据。
`.cg` 让访存直接走 L2，是一种常见的 cache hint 优化手段。
