# 从现有资料出题

目标：把书稿、源码仓库、笔记批量转成符合 quizforge 契约的题目，并保证**事实可追溯**。

## 可用素材位置

**主参考目录：`reference/`**（仓库根的软链 → `/mnt/f/Documents`，**只读**）。
按学科分目录，形态以 PDF 为主（手册 / 论文 / 教科书），夹少量 `.md` 与 `.py`。
切片前先按 `pipeline.md` §2 的分档表处理，**不要指望它整洁**。

| 素材 | 路径 | 形态 |
|---|---|---|
| CUDA / PTX | `reference/cuda/` | `cuda-programming-guide.pdf`、`ptx_isa_9.3.pdf`、`CUDA_C_Best_Practices_Guide.pdf`、`sass_and_gpu_uarch.pdf`、CuTe layout 论文；另有 `layout_polynomials*.md` 与校验脚本 |
| 软件工程论文 | `reference/swe/` | 15 篇 PDF（Lehman 软件演化系列等），IMRaD 结构，适合做迁移题的母本 |
| Linux 教科书 | `reference/linux/` | 《Linux 命令行与 shell 脚本编程大全（第 3 版）》PDF，大部头，按章切 |
| AI Infra | `reference/other/AI-Infra-Book.pdf` | 教科书 |
| 其余学科 | `reference/{amd,c,cxl,ic,math,nvidia,python,riscv,stm32,toolchain,verilog}/` | 尚未盘点体积与形态 |

另有历史素材（QEMU 书稿、CUTLASS、Tenstorrent ISA 等）在 `Codebase/`，
它是指向 `/home/rd/Source/` 的软链，同样是**只读**，不要写入上游仓库。

**材料只读是硬约束**：进 git 的只能是题目；这些材料目录已在 `.gitignore` 里排除。

## 工作流

### 1. 先小批量试跑

不要一上来就抽 200 道题。先挑 **3–5 道**走完整流程（抽取 → 改写 → `check.py` → `build.py`），
确认格式与粒度合适后，再放大批量。

### 1.5 长链条材料：同一条流水线，写成一道长题

遇到长链条的素材（一条技术演化史、一套 API 从朴素到工业级的完整用法），
**不要为它另起一套流程** —— 它在知识空间里就是**一个点**，由该点的 `layers` 决定出成 `problem`。
它的派工、出题、校验、入库与别的题**完全一样**（同一个提示词、同一个校验员、同一个暂存区）。

写成 `problem` 时只有三条**内容**要求（详见 `format.md` §4.6）：

1. **问序不可交换**：每一问必须是上一问解决之后暴露出来的下一个问题。
   把顺序打乱仍能成立，说明它只是题集，应当重排。
2. **至少一个分支点**：问「条件变了该往哪拐」。分支点既给学习者指路，也让背解法的人露馅。
3. **解析要写「为什么接着会有下一问」**：每问的 `source` 指到材料的具体小节，
   整道题就成了一份带索引的读书路线。

小问数量**不设上限**：链条有多长就写多少问。UI 上大题是逐问导航、每问独立作答与批改。

材料与题量再往上走（一本书、一套手册、几百篇论文），改走 `pipeline.md`：
切片分档、覆盖矩阵、任务按知识点打包、独立校验、统合入库。

### 2. 抽取（以 qemu-book 为例）

书稿里的思考题块形如：

```markdown
::::: {.quick-quiz}
把设备型号做成一个 C `enum`，再在 Machine 中用 `switch` 创建，为什么难以支撑 QEMU 当前的配置方式？

:::: {.quick-answer}
中央枚举要求所有可选类型在编译期集中可知，新增模块还要修改公共分支；字符串查找、继承查询、
属性枚举与 QMP introspection 也要另写机制。QOM 让类型自行注册，公共代码通过父类和接口调用。
::::
:::::
```

定位命令（示例）：

```bash
grep -rn "{.quick-quiz}" Codebase/qemu-book/book/chapter*.md
```

统计：该书的 23 章里共有 25 个 `quick-quiz` 块。

### 3. 改写（关键步骤，不要直接搬运）

| 原文特征 | 改写动作 |
|---|---|
| 一句话问答题 | 直接改成 `short`：题面照写，`## 参考答案` 放原文答案，`## 评分要点` 拆成 3–6 条可判定的要点 |
| 有唯一正确答案的辨析题 | 改成 `single` 或 `multi`，**必须自己写 3 个像样的干扰项**，不要凑数 |
| 原文的多个连续问答 | 合并成一道 `problem`，每问一个 `### 小问` |
| 涉及具体代码 / 寄存器 / 提交号 | 保留链接或代码片段，填进 `source` 字段 |

改写时必须：

- **补全推理过程**：原文答案通常只有结论，`## 解析` 要写成能独立读懂的解释。
- **写评分要点**：每条都要能明确判断「命中 / 未命中」，这是 AI 批改的依据。
- **标注出处**：`source` 填章节与文件名，便于回溯核对。
- **术语保留英文原名**（`MemoryRegion`、`prefill`），正文用简体中文。

### 4. 事实核对（不可跳过）

从资料出题最容易出**事实错误**。抽完题后逐条对照原文核对：

- 数值、容量、规格（如 L1 大小、TFLOPS、bank 数）必须与原文/官方文档一致。
- 术语的角色划分（哪个核负责什么、哪个单位管什么）必须与原文一致。
- 不确定的内容**不要写进题目**，宁可换一道。

核对手段：

```bash
grep -rn "关键词" Codebase/<仓库>/ | head -20
```

### 5. 落盘与校验

```bash
cd /home/rd/Desktop/quizforge
python3 tools/new_question.py --topic qemu --type short --chapter "设备模型"
# 编辑生成的文件
python3 tools/check.py questions/qemu/qemu-000X-xxx.md   # 单文件快速自检
python3 tools/check.py                                   # 全库校验
python3 tools/build.py
```

### 6. 汇报

按 topic 汇总新增数量、`check.py` 结果、以及**哪些题目的事实依据比较薄弱需要人工复核**。

## 注意事项

- 一次只处理一个 topic，避免 context 被多份长文档撑爆。
- 抽取时用 `grep`/`search` 先定位候选块，再逐块 `read_file`，不要整本 `read_file`。
- 生成过程中如果发现某道题需要补充新 topic，先改 `meta/topics.yaml`。
- 不要修改 `dist/` 下的产物；不要动 `Codebase/` 下的上游仓库。
