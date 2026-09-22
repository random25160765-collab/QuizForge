# quizforge 的开发 skill

这一批 skill 的**实体就在本仓库里**，仓库自包含：把 `quizforge/` 整体拷走、归档、
或换一台机器打开，它们都跟着走，不会留下指向仓库外的断链。

> **这里只放「开发这个仓库」用的 skill。**
> 出题的那四个层契约（识记 / 理解 / 应用 / 迁移）**不在这里** —— 它们是
> **出题机的内部提示词**，住在 `pipeline/prompts/layers/`。边界见下一节。

## 组成

| skill | 职责 |
|---|---|
| `quizforge-init` | **开局入口**：目录地图、铁律、任务→skill 路由、常用命令、开工/收工清单。新会话先读它，它会把你指向当前状态文件 `docs/STATUS.md` |
| `quizforge-pipeline` | 跑流水线的运行手册：权威在数据库、状态机怎么转、常用命令、卡住怎么判断 |
| `quizforge-author` | 题目**格式契约**：字段、五种题型的正文小节、LaTeX 与代码块写法、校验命令 |
| `quizforge-handmade` | **人工手写单题**的接入：agent 不写题面，只做格式合规、元数据补全、把关系边写成可解析的句式、校验与入库 |

## 四个层契约在 `pipeline/prompts/layers/`

`l1-memorize.md` / `l2-understand.md` / `l3-apply.md` / `l4-transfer.md`。

**为什么和上面那批分开**：它们和「给人读的手册」不是一种东西。这四个文件是
**出题机的提示词** —— `pipeline/worker.py` 的 `load_contract()` 每次出题都把它们读出来
拼进出题员的提示词（插在 `pipeline/prompts/author.md` 的「契约原文」标题下）。
它们的读者是模型，不是人；改一个字的后果是**出题结果变化**，而不是文档过时。

放在 `.codebuddy/skills/` 会同时造成两个误解：一是 `make skills-link` 会把它们链进工作区、
假装成"给人用的 skill"；二是读者无从知道它们同时还是流水线的运行时输入。

每个层契约的重头戏不是「怎么写」，而是**「怎么判断这题是不是真的在这一层」**——
一张判定标准表加一条明确的滑坡警告。例如：

- 识记层：「请说明 X 与 Y 的区别」看着像辨析，但它要求解释机制，那是**理解层**
- 迁移层：最典型的假迁移是「同一道应用题把 $64$ 换成 $128$」——
  真迁移必须改变**问题的结构**，所以迁移契约强制写明「迁移锚点 + 变式手法」

这两条经验值得给人看，但**改规则请改 `pipeline/prompts/layers/` 下的文件** ——
改这里（README）不起作用。

格式契约（`quizforge-author/references/format.md`）则是**两方共用**：出题机按它产出，
人和 agent 也按它校验与修改，所以它留在这边，被 `CONTRACT_FILES` 一起点名。

## `layer` 与 `wing` 为什么要落盘

四层（识记/理解/应用/迁移）与四翼（基础/综合/应用/创新）写在题目 front-matter 里，
但**不出现在界面上**。落盘的理由只有一个：

> 开新 session 出题时，判断「这个知识点已经覆盖了哪几层、哪几翼」只能靠它们。

不落盘的话，agent 对题库的全部认知就是那几十个文件，只能靠猜——
结果是反复出同一层的题、某一翼长期为空。而「同一知识点能分别出四层」
恰恰只能靠覆盖情况来保证。字段定义见 `quizforge-author/references/format.md`。

## 让它们被加载

CodeBuddy 从**工作区的** `.codebuddy/skills/` 里发现 skill。
如果工作区根不是本仓库（比如工作区根是仓库的上一级），在本仓库根跑：

```bash
make skills-link                 # 默认链到仓库的上一级
make skills-link WORKSPACE=~     # 或指定别的工作区根
```

`skills-link` 只建软链，**实体仍在仓库里**（唯一事实来源）。它只处理这里的
四个开发 skill —— 层契约不属于"被加载的 skill"，不进工作区。

## 参考文档

`quizforge-author/references/` 下两份，按需读取、不要一次全读：

- `format.md` —— 字段速查 + 五种题型的完整模板（含代码填空与法式大题）
- `from-source.md` —— 从现有资料（书稿、仓库、笔记）批量出题的流程与注意事项
