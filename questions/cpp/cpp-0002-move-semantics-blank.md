---
id: cpp-0002
type: blank
topic: cpp-value-move
chapter: 值语义与移动
tags: [移动语义, 右值引用, noexcept]
difficulty: 3
source: "C++ 标准 [class.copy.ctor] / Item 29 of Effective Modern C++"
---

阅读下面这段代码，回答两个问题。

```cpp
class Buffer {
public:
    explicit Buffer(std::size_t n) : data_(new char[n]), size_(n) {}

    Buffer(Buffer&& other) /* ??? */ : data_(other.data_), size_(other.size_) {
        other.data_ = nullptr;
        other.size_ = 0;
    }

    ~Buffer() { delete[] data_; }

private:
    char* data_;
    std::size_t size_;
};
```

**第 1 空**：为了让 `std::vector<Buffer>` 在扩容时真正使用移动构造而不是拷贝构造，
上面 `/* ??? */` 处至少应该补上哪个关键字？

**第 2 空**：移动构造执行完毕后，`other` 对象应处于什么状态？（填一个词，例如
用「有效」或「无效」开头的短语都可以，只要是标准要求的那个术语）

## 答案
noexcept
有效但未指定|~有效.{0,8}(状态|值)

## 解析
**第 1 空：`noexcept`。**

`std::vector` 在扩容时需要把旧缓冲区的元素搬到新缓冲区。为了同时提供**强异常保证**
（扩容失败时容器保持原样），它必须知道「搬移不会抛异常」。实现的做法是依据
`std::is_nothrow_move_constructible`（或 `noexcept` 表达式）来选择：

* 移动构造是 `noexcept` → 直接移动，开销是 $O(1)$ 的指针搬运；
* 否则 → 退化用拷贝构造，即使你写了移动构造函数也不会被调用。

所以正确的写法是：

```cpp
Buffer(Buffer&& other) noexcept : data_(other.data_), size_(other.size_) {
    other.data_ = nullptr;
    other.size_ = 0;
}
```

同样的规则也适用于 `std::move_if_noexcept` 与所有提供强异常保证的容器操作。

**第 2 空：有效但未指定（valid but unspecified）状态。**

标准要求被移动对象仍然满足类的不变式、可以安全地析构与赋值，但**不要求**它的值是什么。
因此「移走之后是空」只是本实现（以及 `std::string` 等标准类型的推荐行为）的约定，
标准层面并不保证。这也是为什么不应依赖 `std::move` 之后的源对象内容，
而 `std::unique_ptr`、`std::vector` 这类类型额外提供了 `valid but unspecified` 之外的
更具体保证（如 `unique_ptr` 移动后一定为 `nullptr`）是有意为之的强化。
