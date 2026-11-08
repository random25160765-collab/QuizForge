# QuizForge

一个**本机自己用**的学习台。**对话是前台，题与图是后台。**

单用户、local-first：数据就是一个 SQLite 文件（`data/quizforge.db`），装起来是一个
可执行文件（可选内置 Pyodide 沙箱 —— 在浏览器里真跑 CPython，不是模拟）。

在对话里问、讲、做题、记笔记，都在同一个上下文里；后台是题库与知识图谱 ——
它们回答的是"下一步该学什么、该出什么题"。

## 跑起来

两条命令，从干净克隆到能起服务（实测约半分钟，主要花在装依赖）：

```bash
make api-venv     # 建后端环境（venv + 依赖）
make api-dev      # 构建前端并起服务（**前台阻塞**）
```

干净克隆是**空库**：表由首启自动建好，题是 0。要用自己的题库与历史，把另一台
机器的 `db/quizforge.db.gz.enc` 拷到同路径后 `make db-restore`，或者旧机器
`make bank-export`、新机器 `make bank-import`；日常用户数据走 Turso 同步。
**数据不进版本库**（2026-11-08 起），仓库里只有代码。

然后打开 **<http://127.0.0.1:8100/chat.html>** —— **对话是前台**：
问、讲、做题、记笔记都发生在这一个上下文里，其余页面是它的侧门
（顶层 `/` 也跳到这里）。

`make api-dev` 是**前台阻塞**的（Ctrl+C 停）。要它自己跑在后台：

```bash
nohup make api-dev >/tmp/quizforge.log 2>&1 &
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8100/api/health   # 200 = 起来了
```

**不需要 Docker，也不需要 Postgres。** 数据就是本机一个 SQLite 文件
（`data/quizforge.db`）；表由首启自动建好（alembic 已退役）。
`.env` 可选（`make env-init` 从 `.env.example` 生成，里面是端口与用量上限）。

**数据全在本机，仓库里没有库**：权威是 `data/quizforge.db`；快照
`db/quizforge.db.gz.enc` **就是 SQLite 库本身**、口令加密过的，`make db-restore`
解密解压即用，不用起任何服务。`make db-snapshot` 把它写回 `db/`（**三份都只留本机**，
已 gitignore），用途是**冷备份 / 换机器手动拷贝**。

两台机器日常一起用走 Turso 行级增量同步：设置页「同步」填数据库地址与令牌，或
`make sync`。对话、分组、消息、附件端到端加密后才上传（密钥仍是
`config/snapshot.key`，两台机器必须同一份）；进度、自出的题、设置等其余数据明文上传。
`config/snapshot.key` 由 `make snapshot-key` 生成，**绝不进 git**；落盘快照前还会
自动抹掉 AI 密钥与 Turso token。

要用 AI 相关的功能，得在 `config/ai.local.json` 里填自己的 key
（`baseUrl` / `apiKey` / `model`；这个文件已被 git 忽略，不会跟着仓库走）。

### Windows 桌面入口（可选）

想在 Windows 上"双击就能用"，把这个脚本拷到桌面：

```
tools/desktop-beta.vbs     ← 双击：服务没起就在 WSL 里拉起来，然后开浏览器
```

它调的是 `tools/beta-serve.sh`（**幂等**：已经在跑就什么都不做，顺带构建前端）。
换个机器要改脚本开头的 `DISTRO` 与 `REPO` 两行。

注意这走的是**源码运行形态**（WSL 里的 uvicorn）；打包出来的单文件
（`make package` / `dist-release`）自带一份完整运行时，**不需要 WSL，也别用这个脚本去起它**。

## 七个页面

| 页面 | 干什么 |
|---|---|
| **对话** | 主入口。会话树 + 流式回答；题卡、引用、凭条都长在消息里 |
| **刷题** | 工作台 / 练习 / 组卷 / 复习四个视图 |
| **错题本** | 错题、掌握度、SM2 间隔重复 |
| **知识图谱** | 手写力导向图；概念、语义关系、根因诊断 |
| **资料** | 只读的资料来源库：材料、切片、条目与引用 |
| **设置** | 全屏设置页：AI / 联网搜索 / 外观 / 数据 |
| **主页** | 暂时是空白页 —— 资源栏与窗格组合已移除，等 Obsidian 桥接接上（见 `docs/Obsidian桥接协议.md`） |

> 2026-11-07：自研笔记页（写作台 / 画布 / 笔记图谱）与左侧资源栏整块删除 ——
> 笔记改为**直接桥接本机 Obsidian**；LLM 侧的笔记读写工具保留（`read_note` /
> `list_notes` / `search_notes` / `edit_note` / `write_note`），它们将改走桥接协议。

## 它怎么想事情

agent 把"推导"的边际成本压到近零，于是**验证与审读成了不能外包的环节**。
所以这里不是"考你背得全"，而是"考你审得出"：

- **四层**（识记 / 理解 / 应用 / 迁移）—— L1+L2 出满，L3+L4 少而精，且必须人审；
- **知识空间** —— 材料 → 点 → 概念 → 关系；题挂在概念上，
  **缺题清单就是"下一步出什么"的依据**；
- **出题流水线** —— 派工 → 出题 → 独立校验 → 入库。钱可忽略，人的时间才是瓶颈。

前台与后台是**同一个上下文**：讲到一个点就能推题、做完能立刻回对话里问，
笔记与资料反过来给对话当上下文。所以题卡不是结尾，它常常是下一轮对话的开头 ——
答完点「发送」，作答就回到对话里，由本页这个模型批改、接着讲。

## 能力挂载

输入区那颗药丸决定"这一版对话能调什么"，三档：

| 模式 | 能干什么 |
|---|---|
| **极简** | 不挂任何工具，纯聊天 |
| **查询** | 只读：笔记、资料、知识图谱 |
| **学习** | 全挂（推题、跑 Python、派子代理都在） |

粒度是"**组 × 权限档**"（`read` / `propose` / `write` / `exec`）—— 光靠"组"筛不出
"只读"。默认学习模式。会改状态的那两件（收藏、标掌握）永远是**模型提案、人按确认**。

## 权威在哪

**题目、考纲、知识图谱都在数据库里 —— 仓库里没有题目文件，也没有数据库副本。**

数据**不进版本库**：库只在本机 `data/quizforge.db`，快照只在本机 `db/`。
校验与测试会把库**物化到临时目录**再读，避免用到跨轮残渣。
交换形态有 `make bank-export` → `bank.json`（题库投影）和 `make user-export`
（用户数据包）：都是按需生成的投影，不进版本库；日常跨机器用 Turso 行级同步。

内容来源是几个只读目录（材料 PDF、笔记 vault、文档仓库）—— 它们**只读**，
写回去的只有库里那一份。

## 常用命令

```bash
make check        # 密钥扫描 + 题库校验（0 error 是硬线）
make test         # 语法检查 + 前述校验 + 前端断言
make graph-check  # 知识图谱体检
make coverage     # 哪些材料出过题、还剩多少点
make drive        # 出题流水线：派工 → 出题 → 校验 → 发布 → 打回
make package      # 打单文件应用（另有 dist-linux）
make help         # 全部命令
```

## 文档

| 文档 | 定位 |
|---|---|
| [`docs/THESIS.md`](docs/THESIS.md) | 业务立论：所有设计取舍的根据 |
| [`docs/DESIGN.md`](docs/DESIGN.md) | 工程决策与踩过的坑 |
| [`docs/STATUS.md`](docs/STATUS.md) | **唯一易变落点**：现在什么形态、题库与图谱多大、未决事项 |
| [`docs/TUTORIAL.md`](docs/TUTORIAL.md) | 使用与运维说明书 |
| [`AGENTS.md`](AGENTS.md) | 仓库硬约定（提交规范、材料只读、题目是投影） |
| [`.codebuddy/skills/`](.codebuddy/skills/README.md) | 出题契约与流水线流程（跟代码一起改） |

**权威在数据库与代码，不在文档里。** 文档解释"为什么"；"现在是什么状态"看
`docs/STATUS.md`，或者直接问：

```bash
make check       # 题库校验（0 error 是硬线）
make coverage    # 知识空间还缺哪些题
```
