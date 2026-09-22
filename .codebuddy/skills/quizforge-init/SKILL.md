---
name: quizforge-init
description: 接手 quizforge 时的开局入口——项目定位、目录地图、不可违反的铁律、任务→skill 路由表、常用命令与开工检查清单，并指明「当前进展」该去哪里读。当开始新会话、刚进入这个仓库、或需要快速搞清楚这个项目在干什么 / 下一步做什么 / 某件事该走哪个 skill 时使用。
---

# quizforge 开局

## 先读这两个

1. **`docs/STATUS.md`** —— 当前进展、未决事项、待建工具。**这是唯一易变的落点**，每次开工先读、收工更新。
2. **`.codebuddy/skills/README.md`** —— 五个 skill 的分工（很短）。

**不要一上来把所有 skill 都读一遍。** 按下面的路由表，只取当前任务需要的那一份。

## 这是什么项目

**local-first 单机**的学习台：对话是前台，题与图是后台。FastAPI + **SQLite**
（数据就是 `data/quizforge.db` 一个文件）提供数据，前端产物落在 `api/web/`，
是普通静态资源 + `/api` 调用。**没有账号** —— 连 `users` 表与 `user_id` 外键
都在 2026-09-22 整体拆掉了（见 `docs/STATUS.md` §五）。

`store.js` 对外始终是同步接口（本地先写、后台按流水增量回传），所以 UI 调用点不必关心同步。

（早先还有一条「离线单文件：题库内联进 HTML、双击即用」的形态 —— 它随
「题库权威迁到数据库」一起淘汰了：那要求把整库与字体 base64 化，并放弃服务端才有的一切。）

## 目录地图

| 路径 | 是什么 |
|---|---|
| `data/quizforge.db` | **权威在库**：题库 / 考纲 / 知识空间 / 材料都在这个 SQLite 文件里，仓库里**没有**题目的文件形态 |
| `db/quizforge.db.gz` | 库的快照（**就是库本身**，不是导出版本）；`make db-snapshot` 存、`make db-restore` 解压即用 |
| `tools/` | `check.py` 校验 · `build_web.py` 构建前端（入口）· `assemble.py` 外壳装配 · `question_parser.py` 解析 · `topics.py` 考纲解析 · `new_question.py` 脚手架 · `db_snapshot.py` 快照存取 |
| `theme/` | 前端运行时：`app.css` + `runtime/*.js`；改完要 `make web`（新增脚本还要登记进 `build_web.py` 的 `RUNTIME_ORDER`）|
| `api/` | FastAPI 后端：题库导入、进度增量同步、AI 转发（用本机设置里那份密钥）|
| `.codebuddy/skills/` | 出题 skill（本仓自包含，跟着仓库走） |
| `docs/` | `STATUS.md`（当前状态）· `THESIS.md`（**业务立论**：为什么这么设计）· `DESIGN.md`（设计说明）· 截图 |
| `draft/` | 人写的手写题与草稿（与机器生成的 `maps/` 分开） |
| `maps/` | 机器生成的切片索引、覆盖矩阵与暂存区（可重建） |
| `reference/` | **仓库外**的只读软链：手册、论文、教科书（指向哪由本机决定，不进版本库）|
| `Codebase/` | **仓库外**的只读软链：QEMU 书稿、CUTLASS、Tenstorrent ISA（同上）|

## 铁律（违反会出事）

1. **材料只读。** `reference/` 与 `Codebase/` 都在 `.gitignore` 里；进 git 的只能是题目。
2. **库是权威，其余都是投影。** `api/web/`（构建产物）、`bank.json`（导出）都是投影，**不要手改**；
   改题目改库，然后重新物化构建。
3. **改完题必须** `make check` 到 **0 error**（它先把库物化到临时目录再校验）。
   契约里写明：只有 `[OK] ... 全部通过` 才算完成；`make test` 再补一层前端自测。
4. **出题依据的优先级：原材料内容 + 用户诉求 > 考纲。** 考纲只作归类、可随时重构；
   绝不为填考纲出题，也不因考纲没有合适的 key 就不出题（顺序是「先出题 → 最后归类 → 兜不住就改考纲」）。
5. **层契约的判定表是审核项，不是建议。** 每道题都要能通过对应层的那张表。
6. **`problem` 的问数不设上限**（原「建议 4–10 问」已删）；但每问必须能独立判分。
7. **迁移层的题必须请用户过目** —— 「是不是真迁移」只有人读得出来。

## 任务 → 该加载哪个 skill

> 出题的四层契约在 `pipeline/prompts/layers/` —— 它们是**出题机的内部提示词**
> （`pipeline/worker.py` 每次出题把它们读进提示词），不是 CodeBuddy skill，
> 所以不在 `.codebuddy/skills/` 里。

| 要做的事 | 先读 |
|---|---|
| 出识记层题（术语、数值、枚举） | `pipeline/prompts/layers/l1-memorize.md` |
| 出理解层题（换说法、新情形、反例、**审读**） | `pipeline/prompts/layers/l2-understand.md` |
| 出应用层题（按规程算出确定结果） | `pipeline/prompts/layers/l3-apply.md` |
| 出迁移层题、综合大题 | `pipeline/prompts/layers/l4-transfer.md` |
| **人工手写了一道题**（尤其法式大题），要接进题库与图谱 | `quizforge-handmade` —— 它注入格式、元数据与「可解析出关系边」的句式，保证不成孤岛 |
| 查字段怎么填、确认某题格式对不对 | `quizforge-author` + `references/format.md` |
| 从一份材料出一批题（几道） | `references/from-source.md` |
| 面对一本书 / 一套手册 / 几百篇论文批量出题 | `references/pipeline.md` |
| 拿不准某个设计取舍该往哪边倒 | `docs/THESIS.md`（业务立论——所有取舍的根据） |
| 改 UI | 先看 `docs/STATUS.md` 的 UI 约定；再改 `theme/`（UI 无 skill，规则写在代码注释里） |
| 后端 / 同步 / 入库 | 直接读 `api/app/`（无 skill） |
| **跑流水线 / 补缺口 / 查为什么没进度** | **`quizforge-pipeline`**（状态机、常用命令、卡住怎么办 ✗ —— 先读它再动手 ✔） |

## 常用命令

```bash
make help         # 目标清单（按用途分组，含流水线那几条）
make coverage     # 覆盖率对账：哪些材料出过题、还剩多少点
make drive        # 状态机自动跑：出题 → 校验 → 发布 → 打回重出，收敛即停
make check        # 题库校验，必须 0 error（题库来自数据库，物化到临时目录后即删）
make test         # 校验 + 前端逻辑自测（判分 / 渲染 / SM2 / 掌握度 / 合并口径）
make web          # 前端 → api/web/
make db-restore   # 解压快照 → data/quizforge.db（首启要有题就跑它，不需要 Docker）
make api-dev      # 后端热重载（127.0.0.1:8100）
make api-test     # 后端 pytest
make new TOPIC=<主题> TYPE=<题型>   # 生成一道新题骨架（另加 --title/--difficulty 需走 python3 tools/new_question.py）

# 题库入库（先看影响面，不写库）
cd api && .venv/bin/python -m app.cli.import_bank --dry-run
```

## 开工检查清单

- [ ] 读 `docs/STATUS.md`（当前进展与未决事项都在那儿）
- [ ] `make check` —— 确认题库基线还是绿的
- [ ] 需要在线版时：`curl -s localhost:8100/api/health`（起服务用 `make api-dev`）
- [ ] 把本次任务对到上面那张路由表，**只加载对应的那一个 skill**
- [ ] 若这批题涉及新材料：先按 `pipeline.md` §2 的分档切法产出骨架，**给用户过一眼再往下切**

## 收工清单

- [ ] `check.py` 输出（贴原始输出，不要只写"通过"）
- [ ] 改动了哪些文件；新增/修改了几道题
- [ ] **哪几处事实依据薄弱、需要用户人工复核**（每条 skill 都强制要求汇报这一项）
- [ ] 更新 `docs/STATUS.md` 里会变的部分（题库规模、缺口、未决事项）
- [ ] 未提交的改动要不要提交（**必须问，不要自己提交**）
