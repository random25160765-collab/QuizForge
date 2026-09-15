---
id: qemu-0001
type: single
topic: qemu-device-memregion
chapter: 设备模型与内存
tags: [MemoryRegion, MMIO, 地址空间, 设备模型]
difficulty: 3
source: "QEMU 文档 docs/devel/memory.rst / qemu-book 第 4、5 章"
---

在 QEMU 的设备模型中，`MemoryRegion` 是描述「一块可被 CPU 或设备访问的地址区间」的核心对象。
关于它的用法，下列说法正确的是？

## 选项
- A. 一块 `MemoryRegion` 必须同时提供读写回调；只有回调全部实现后才能真正接入地址空间
- B. 用于 MMIO 的 `MemoryRegion` 通常用 `memory_region_init_io()` 创建并挂上 `MemoryRegionOps`；需要映射宿主内存时用 `memory_region_init_ram()`；把子区域拼进父区域用 `memory_region_add_subregion()`
- C. `MemoryRegion` 一旦创建就自动出现在系统地址空间中，不需要显式添加
- D. 一个 `MemoryRegion` 只能对应一个连续区间，因此无法表达「父区域带有漏洞、由子区域覆盖」这类重叠结构，重叠必须由设备自己处理

## 答案
B

## 解析
`MemoryRegion` 是 QEMU 里唯一的地址空间抽象，CPU 访问、设备 MMIO、DMA
都统一成「往某个地址空间里的某个 Region 发请求」。

**B 正确**，这也是最常用的三类构造方式：

```c
typedef struct MyDev {
    MemoryRegion iomem;      /* MMIO 区域，暴露 CSR */
    MemoryRegion ram;        /* 设备内部的 RAM 后备存储 */
    MemoryRegion *sysmem;
} MyDev;

static const MemoryRegionOps my_ops = {
    .read  = my_read,
    .write = my_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 4, .max_access_size = 4 },
};

static void my_realize(DeviceState *dev, Error **errp) {
    MyDev *s = MY_DEV(dev);
    memory_region_init_io(&s->iomem, OBJECT(s), &my_ops, s, "my-dev-mmio", 0x1000);
    memory_region_init_ram(&s->ram, OBJECT(s), "my-dev-ram", 0x10000, errp);
    sysbus_init_mmio(SYS_BUS_DEVICE(dev), &s->iomem);
}
```

**A 错。** `MemoryRegionOps` 的 `read` / `write` 是**可选的**：只读区域可以只给 `read`，
只写区域可以只给 `write`。未提供的方向会被 QEMU 统一处理（例如对只读区域写入会
触发错误或忽略）。另外 `.valid` 用来约束合法访问宽度与对齐，配错比不配更容易出问题。

**C 错。** 创建只是「造出一块区域」，必须通过 `memory_region_add_subregion()`
（或 sysbus 的 `sysbus_init_mmio()`、PCI 的 BAR 映射）把它挂到某个地址空间上，
CPU 才看得到。这也是为什么很多设备要显式接收一个 `MemoryRegion *sysmem` 参数。

**D 错。** 恰恰相反，`MemoryRegion` 支持**层次化嵌套**：父区域可以有自己的
读写动作，同时把一部分区间「挖」出去交给子区域。QEMU 会按优先级（priority）和
添加顺序决定重叠时的胜者。PCI 的 BAR、SoC 里的多个外设、以及需要
「先拦截再转发」的场景都依赖这个能力。

一个排错小技巧：`info mtree`（monitor 命令）可以打印出当前的地址空间树，
是定位「设备映射位置对不对」「是不是被别的区域覆盖了」最直接的手段。
调试内存问题时，先看 `info mtree -f` 的输出，再回去查代码，通常比读代码快得多。
