# AGENTS.md —— 在本仓库工作的约定

写给 AI agent（也顺便当新人的速查）。**这里只放「怎么做」的硬约定**，
项目是什么、当前进展、为什么这么设计，分别在：

| 想知道 | 读哪 |
|---|---|
| 项目定位、任务→skill 路由、开工/收工清单 | `.codebuddy/skills/quizforge-init/SKILL.md` |
| 当前进展、未决事项、待建 | `docs/STATUS.md` |
| 业务立论（一切取舍的根据） | `docs/THESIS.md` |

---

## 提交规范

Conventional Commits 的简化版。**commit message 一律用英文**（仓库文档是中文，两者不冲突）。

### 格式

```
<type>(<scope>): <subject>

<body: why it changed; what only when non-obvious>

<footer: BREAKING CHANGE / Refs>
```

### type

| type | 用于 |
|---|---|
| `feat` | 新能力（学习者可见） |
| `fix` | 修 bug |
| `content` | **题库与考纲的内容变更**：加题、改题、调难度、改解析 |
| `docs` | 面向人的文档（README / STATUS / THESIS / DESIGN / AGENTS） |
| `refactor` | 不改行为的重构 |
| `test` | 测试与自测 |
| `build` | 构建、打包、镜像、依赖 |
| `chore` | 其它杂务 |

### scope（固定集合，不要自创）

`questions`（题库）· `meta`（考纲主题树）· `tools`（解析 / 校验 / 构建 / 脚手架）·
`theme`（前端运行时）· `api`（后端与同步）· `pipeline`（出题流水线、编排、执行器、检索层）·
`skills`（skill 本身：契约、四层判定、init、handmade）· `docs`（文档）· `repo`（仓库级配置）

### subject

- **英文**，祈使/陈述语气，**不加句号**，不超过 50 字符
- 说「做了什么」，不写「I changed…」
- **一条提交只做一件事**：subject 里出现 `and` / `plus` / `also` / `;` —— 说明该拆
- **scope 可省略**：只涉及文档（如 `docs: record the business thesis`）或横跨多域时省略

### body

- 写 **why**。diff 已经说明了 what，正文重复 diff 是噪音
- 涉及取舍时写清被否决的方案与原因（"did not do X because Y"）
- 一段或几行，不用列表硬凑

### footer

- `BREAKING CHANGE: <说明>`（同时 subject 末尾加 `!`）
- `Refs: <文件或链接>`（可选）

### 例外

`docs/STATUS.md` 是流水账，**随对应那条提交一起走**，不单独提交。

### 示例

```
content(questions): add 5 cuda apply-level questions on tiling and bank conflict

The bank covered this section in one sentence and had nothing on bank
conflict. Written against the l3 rubric: self-consistent parameters and
full intermediate steps in the explanation.

Refs: reference/cuda/cuda-programming-guide.pdf §9.2
```

```
docs(pipeline): settle the retrieval layer and the authoring orchestration
```

反例（都是真实犯过的）：

- `出题方针与批量流水线落盘` —— 无 type/scope、非英文，且两件事混在一起
- `新增开局入口：quizforge-init skill + docs/STATUS.md；draft/ 入库` —— 三件事用「；」串起来
- `落盘业务立论 docs/THESIS.md，并在四处挂指针` —— 「并」字一出现就该想想能不能拆

---

## 提交与推送的硬规则（agent 必须遵守）

1. **未经用户明确许可，不要 commit。** 提交前把 subject 给用户看一眼。
2. **不要 force push 到 main**，除非用户明确要求；即便要求，也必须用 `--force-with-lease`，
   并把被覆盖的旧 SHA 打印出来（回滚依据）。
3. **提交前先 `git status`**：绝不能把密钥或材料带进版本库——
   `config/ai.local.json`、`.env`、`reference/`（材料软链）、`dist/`、`api/web/`、`dist-local/`。
4. 一次提交一件事；拿不准就拆。
5. 改完题库必须 `python3 tools/check.py` 到 **0 error**，再 `python3 tools/build.py`。

## 其它硬规则

- **材料只读**：`reference/` → `/mnt/f/Documents`、`Codebase/` → `/home/rd/Source/` 都是只读软链，
  进 git 的只能是题目
- **题目是唯一事实来源**：`dist/`、`api/web/`、数据库都是它的投影，不要手改
- **出题依据优先级**：原材料内容 + 用户诉求 > 考纲；考纲只作归类、可随时重构
- **layer / wing 不许留空**：它们是学习算法的坐标轴（见 `docs/THESIS.md`）
