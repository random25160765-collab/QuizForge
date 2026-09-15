---
id: cpp-0003
type: blank
topic: cpp-value-move
chapter: 值语义与移动
tags: [Rule of Five, 拷贝控制, 代码填空]
difficulty: 4
blankMode: code
blankLines: 8
caseSensitive: false
source: "C++ Core Guidelines C.21 / 代码填空题型示例"
---

下面是一个手工管理堆内存的类。请把缺失的**移动构造**与**移动赋值**补全，
使其满足 Rule of Five，并保证被移动对象处于可安全析构的状态。

```cpp
#include <cstddef>
#include <utility>

class Buffer {
public:
    explicit Buffer(std::size_t n) : data_(new char[n]), size_(n) {}
    ~Buffer() { delete[] data_; }

    Buffer(const Buffer& other)
        : data_(new char[other.size_]), size_(other.size_) {
        std::copy(other.data_, other.data_ + size_, data_);
    }

    Buffer& operator=(const Buffer& other) {
        if (this != &other) {
            char* fresh = new char[other.size_];
            std::copy(other.data_, other.data_ + other.size_, fresh);
            delete[] data_;
            data_ = fresh;
            size_ = other.size_;
        }
        return *this;
    }

    /* ---- 请补全下面两个函数 ---- */

    // 移动构造：接管 other 的资源，并让 other 进入可安全析构的状态
    Buffer(Buffer&& other) ____________________ : data_(other.data_), size_(other.size_) {
        other.data_ = nullptr;
        other.size_ = 0;
    }

    // 移动赋值：先释放自身资源，再接管 other 的资源，并处理自赋值
    Buffer& operator=(Buffer&& other) ____________________ {
        if (this != &other) {
            delete[] data_;
            data_ = other.data_;
            size_ = other.size_;
            other.data_ = nullptr;
            other.size_ = 0;
        }
        return *this;
    }

private:
    char* data_;
    std::size_t size_;
};
```

**第 1 空**：移动构造函数参数列表后、初始化列表前，需要补上什么关键字/说明符？
（只填那一个关键字）

**第 2 空**：移动赋值函数体的左花括号前，需要补上同样的东西。填同一个关键字即可。

## 答案
noexcept
noexcept

## 解析
**两个空都填 `noexcept`。**

补全后的签名是：

```cpp
Buffer(Buffer&& other) noexcept : data_(other.data_), size_(other.size_) { ... }

Buffer& operator=(Buffer&& other) noexcept { ... }
```

**为什么必须写。** 移动操作本身只做指针与整数的搬运，不会失败，因此
「不抛异常」是事实；但**编译器不会替你推断**这件事——你手写的函数默认是
potential throwing 的。必须显式写 `noexcept`（或 `noexcept(true)`）。

**写在移动构造上为什么关键。** `std::vector` 在扩容时需要在
「强异常保证」与「性能」之间取舍。它的实现依据
`std::is_nothrow_move_constructible`（或 `std::move_if_noexcept`）判断：

| `T` 的移动构造 | `vector` 扩容时选择 | 代价 |
|---|---|---|
| `noexcept` | 移动元素 | 指针搬运，$O(1)$ |
| 可能抛异常 | **拷贝**元素 | 深拷贝，可能 $O(n)$ |

所以漏掉 `noexcept` 的后果不是「编译不过」，而是**你的移动构造函数永远不会被
vector 调用**，`std::vector<Buffer>` 扩容会静默退化成深拷贝，
在性能敏感的热路径上造成数量级的差异，而且极难排查。

**移动赋值里还要注意的两点。**

1. **自赋值保护**：`if (this != &other)`。若省略，`delete[] data_` 之后
   `data_ = other.data_` 读到的就是已经释放的内存。
2. **被移动对象的状态**：标准只要求移动后源对象处于「有效但未指定」状态，
   不要求为空。但**显式置空**（`data_ = nullptr; size_ = 0;`）是自定义类的
   最佳实践：它让析构安全，也让「移动后再次使用」的 bug 表现为空指针而不是
   悬垂指针。注意 `std::size_t` 是内置类型，`other.size_ = 0` 必须在函数体里
   显式写，不会自动清零。

**为什么不是 `const` / `inline` / `override`。** `override` 只用于虚函数重写，
这两个函数不是虚函数；`inline` 与异常规格无关；`const` 会破坏「修改 other」的语义。
所以唯一正确的是 `noexcept`。
