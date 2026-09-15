---
id: rust-0001
type: multi
topic: rust-own-borrow
chapter: 所有权与借用
tags: [borrow checker, 借用规则, 可变引用]
difficulty: 2
source: "The Rust Programming Language, ch.4 / Rust Reference: References"
---

关于 Rust 的引用规则，下列说法正确的是哪些？

## 选项
- A. 在任意时刻，对同一份数据要么存在任意多个共享引用 `&T`，要么存在唯一一个可变引用 `&mut T`，两者不能同时存在
- B. 一个可变引用只能通过 `&mut` 创建，而且创建它的前提是这条路径上没有任何活跃的共享引用
- C. 引用的生命周期（lifetime）必须覆盖它的每一次使用，Borrow Checker 以「非词法生命周期」的活跃区间来判断
- D. 只要变量的作用域还没有结束，它创建的引用就一定仍然存活，因此无法在作用域中途重新借用
- E. `&mut T` 是 `Copy` 的，因此可以像 `&T` 一样自由地复制多份并同时使用

## 答案
A,B,C

## 解析
逐项分析：

**A 正确。** 这就是借用规则的核心表述，也是数据竞争在编译期被消除的根本原因：
读共享、写独占。

**B 正确。** `&mut` 的获得方式只有 `&mut place`（或 `&mut *ptr` 之类的重借用），
并且要求该路径上不存在活跃的共享引用——否则编译器会报
`cannot borrow ... as mutable because it is also borrowed as immutable`。

**C 正确。** 自 Rust 2018 起使用**非词法生命周期（NLL）**：编译器以控制流图为依据，
计算每个引用的**活跃区间**（live range），而不是简单按作用域判断。因此下面这段是合法的：

```rust
let mut v = vec![1, 2, 3];
let first = &v[0];
println!("{first}");
v.push(4);          // 合法：first 的活跃区间在此处已经结束
```

**D 错误。** 正是 NLL 推翻了这种说法。引用的存续期由「最后一次使用」而不是
「外层变量的作用域」决定。上面的例子如果不打印 `first`，或者打印发生在外层作用域末尾，
才会报错。

**E 错误。** `&mut T` **不是** `Copy`。如果它是 `Copy`，就可以同时持有两个可变引用，
独占性立刻被破坏。`&mut T` 可以移动（move），也可以被重新借用（reborrow，形如 `&mut *r`），
但同一时刻只能有一份有效。

补充一个常被混淆的点：`&T` 是 `Copy` 的，所以把共享引用传进函数不会转移所有权；
`&mut T` 传进函数则是移动，函数返回后如果想继续用，需要显式地把引用返回出来或者
依赖 NLL 的重借用规则。
