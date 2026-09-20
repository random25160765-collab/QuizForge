# 说明书

使用与运维。门面见 [README](../README.md)，工程取舍见 [DESIGN.md](DESIGN.md)，
业务立论见 [THESIS.md](THESIS.md)，出题契约与流程在 [`.codebuddy/skills/`](../.codebuddy/skills/README.md)。

> **文档会过期。** 这份说明书只讲"怎么用、怎么运维"；凡是会变的量
> （题量、覆盖率、缺口、流水线积压）一律现场问：
>
> ```bash
> make check        # 题库校验（0 error 是硬线）
> make coverage     # 知识空间铺到哪、还缺什么
> api/.venv/bin/python -m pipeline.status   # 流水线队列与账本
> ```
>
> 看到文档里写死的数字，请当作"当时如此"。

---

## 一、安装

### 前置

| | 要求 |
|---|---|
| 数据库与部署形态 | Docker + Docker Compose |
| 构建 / 校验 / 流水线 | Python 3.12+（只依赖标准库 + PyYAML 等轻量件）|
| 前端 | **不需要 Node**（运行时是手写 JS 按固定顺序拼接）|

### 在线版（本机开发）

```bash
make api-venv      # 后端依赖 → api/.venv
make env-init      # 生成 .env（没有密钥要填）
make db-up         # PostgreSQL → 127.0.0.1:5432
make db-restore    # 灌入 db/quizforge.sql.gz（题库 + 考纲 + 知识空间）
make web           # 构建在线前端 → api/web
make api-dev       # → http://127.0.0.1:8100
```

打开页面即可开始（没有注册账号那一步）。库里空时按下面「从快照搬题」灌一次。

### 从快照搬题（可选）

仓库里带着一份快照 `db/quizforge.sql.gz`（Postgres dump）。它不进应用，
只是**搬历史数据**的入口 —— 搬完落到本机的 `data/quizforge.db`：

```bash
make env-init
make db-up         # 起一个 Postgres 容器当源库
make db-restore    # 用快照灌进去
python tools/migrate_to_local.py    # 逐表搬到 SQLite，搬完对账；之后 make db-down
```

更省事的办法是直接拷一份现成的 `data/quizforge.db` —— local-first 的正路。

首次准备 KaTeX（只从本机副本同步，不联网）：

```bash
make vendor
```

> 早先还有一条「离线单文件：全部内联、双击即用」的形态，已随「题库权威迁到数据库」淘汰 ——
> 它的前提（把自己内联成一个自包含文件）与现在的能力（账号、同步、图谱、AI 批改）不相容。

---

## 二、使用

### 工作台

今日战况（已做 / 正确率 / 连续天数）、**刷题热力图**、主题卡片与推荐。

- 热力图走 GitHub 的形态：周为列、五档配色；顶部可按主题筛选，
  筛选后右侧统计数字一起变。**筛选项只列你练过的学科**——没练过的点进去必然是空图。
- 连续天数按"这天有没有作答"算；今天还没做时不归零，从昨天往前数。
- 主题卡片右键可置顶。

### 练习

分成**选题**与**做题**两个互斥状态：

**选题态** —— 左栏是筛选条件 + 完整题目卡片（题面、选项、答案都能当场看完），
每张卡三个动作：`显示答案` / `加入选题篮` / `单题练习`；右栏是选题篮，攒够点「开始练习」。
筛选项：主题树（学科 → 单元 → 知识点）、题型、难度、掌握度、收藏夹、关键词。

**做题态** —— 只有题目卡片与右侧题号列表，底部出现答题状态栏；逐题作答即时判分并展开解析。

### 组卷

按主题抽题、设定题量与时限，计时作答；交卷后给出总分、分主题正确率、用时与需复盘清单。
试卷草稿实时落盘，中途退出可以从「组卷」页继续。

### 复习

基于 **SM2 间隔重复**：四档自评（重来 / 困难 / 一般 / 简单）更新下次复习时间，
并给出未来 14 天的复习负载图。到期的题会出现在「复习」页与工作台入口角标上。

### 错题本

独立页面，汇总答错或**答得不完整**的题（多选漏选、填空只对一部分、AI 判为部分正确都会进来）。
支持按主题筛选、原地重做、一键重刷、导出 Markdown、打印。
最近一次作答正确后自动移出默认列表，可切换「含已订正」查看。

### 知识图谱

`graph.html`。画布是力导向图，节点是**概念**（不是点），边是概念间的语义关系。

- 浮动设置面板：搜索概念名 / key、按主题筛、按节点类型与关系类型过滤、调力导向参数、"重排"
- 点节点开右侧详情：定义、事实、关系列表；`只看邻域` 收窄到一跳邻居，`看全图` 还原
- 数据优先取 `/api/graph`（实时），取不到退回 `graph.json` 快照（服务端异常时的降级）
- 节点与边都可逐类开关：节点默认只显示**概念**与考纲节点（材料节点关）；
  边默认只开前置 / 组成 / 实现与考纲层级，**易混默认关**——它有七千多条、
  由词面匹配产出、精度偏松，打开就把图压成一团，要看时点一下胶囊即可
- 考纲里那些"点的镜像叶子"默认隐藏

### 设置

右上角齿轮。三组：

| 组 | 项 |
|---|---|
| AI 批改（简答题） | 启用开关 · 要求模型返回 JSON · 失败时降级为自评 · 接口地址 · 模型名 · 密钥 · 测试连接 |
| 外观 | 浅色主题 |
| 数据 | 导出全部数据 · 导入数据 · 清空本机数据 |

密钥是**你自己的**：填在设置里，在线版存在你自己的账号下（换设备不用重填），
服务端只做转发 —— 不少模型供应商不允许浏览器直连。

不想用云服务可以接本地 Ollama：

```bash
OLLAMA_ORIGINS=* ollama serve     # 接口地址填 http://localhost:11434/v1
```

### 快捷键

只在**做题态**接管键盘（选题态下方向键照常滚动题库）：

| 键 | 作用 |
|---|---|
| `1`–`9` | 选选项（单选 / 多选）|
| `Enter` | 提交 / 下一题（简答题用 `Cmd/Ctrl + Enter` 提交）|
| `←` `→` | 上一题 / 下一题 |

打字时、模态打开时不接管。

### 掌握度

每道题有一个 **0–100 的掌握度**，由做题记录算出，比"错没错"更能说明问题：
做对会升、长时间不碰会回落。实现见 `theme/runtime/store.js`，
服务端是 `api/app/mastery.py`，两侧由 `meta/mastery-fixtures.json` 锁一致性。

```
acc   = (correct + 1) / (attempts + 2)      平滑后的正确率
ev    = attempts / (attempts + 3)           证据强度：做得少就向中间收缩
fresh = 0.5 ^ (days / 30)                   30 天半衰期的记忆新鲜度
base  = acc × (0.55 + 0.45 × fresh) × (0.6 + 0.4 × ev)
加成   = min(streak, 3) × 3                  连续答对最多 +9
```

档位五档：`未练 < 40 薄弱 40–69 一般 70–89 熟练 ≥90 精通`，练习页可按档位筛选。
**星标是另一回事**：掌握度是算出来的，星标是手动置的。

---

## 三、数据

### 权威在哪

**数据库是唯一权威**：题目、考纲、知识空间、图谱全部以 PostgreSQL 为准。
仓库里没有题目文件，`api/web/`（前端产物）、`bank.json`（导出）、`graph.json`（图谱快照）都是**投影**
—— 改题改库，然后重新物化构建；**不要手改投影**。

### 快照与备份

```bash
make db-dump        # → db/quizforge.sql.gz（这是唯一进版本库的数据形态）
make db-restore     # 从它恢复
make db-backup      # → ~/quizforge-backups/quizforge-<时间戳>.sql.gz（仓库外）
```

规矩是：**数据进版本库只走快照**。改了题、补了概念、跑完流水线，就 `make db-dump` 一次，
把 `db/quizforge.sql.gz` 一起提交 —— 换台机器 `make db-restore` 就能接着干。

### 文件形态（导入导出）

题目另有文件形态，但它只是**交换格式**，不是来源：
`make bank-export` → `bank.json`，`make bank-import` 反向。
单题的手写（法式大题这类）走 `quizforge-handmade` skill，它管格式补全、
关系边解析与入库影响面。

### 多设备与断网

- 每次判分产生一条**流水**（增量），服务端只对真正插入成功的流水累加 ——
  两台设备同一天的练习是**相加**，不是互相覆盖。
- 切回前台会自动推一次、拉一次最新进度（做题过程中不打断你）。
- 断网时仍可作答：进度先落本机（localStorage），联网后自动补传。

---

## 四、出题

### 走 skill

出题规则在 [`.codebuddy/skills/`](../.codebuddy/skills/README.md)（跟着仓库走，改了就在眼前）：

| skill | 管什么 |
|---|---|
| `quizforge-init` | 开局入口：目录地图、铁律、任务→skill 路由 |
| `quizforge-author` | 格式契约：字段、五种题型的正文小节、LaTeX 与代码块写法、校验命令 |
| `quizforge-l1-memorize` / `l2-understand` / `l3-apply` / `l4-transfer` | 四层各自「这题算不算这一层」|
| `quizforge-handmade` | 人写的单题接进题库与知识图谱 |
| `quizforge-pipeline` | 流水线怎么跑、卡住怎么判断、覆盖率怎么看 |

说清**层**，agent 就会走到对应的 skill。"怎么判断这题真在这一层"是每个层 skill 的重头戏
（例如「只把 $64$ 换成 $128$」是假迁移）。

### 四层与四翼

- `layer` 四层：**识记 / 理解 / 应用 / 迁移** —— L1+L2 出满，L3+L4 少而精且必须人审
- `wing` 四翼：写在题目 front-matter 里，**不进界面**（理由见 skills 的 README）

题量是材料知识密度的函数，不是预算：不预先规定 L1:L2:L3:L4 的比例。

### 校验是硬线

```bash
make check     # 全库校验（含草稿），必须 0 error
make test      # 题库校验 + 前端逻辑自测（判分 / SM2 / 掌握度 / 持久化 / 合并口径）
```

改判分或数据口径前先 `make test`。校验与构建都会**先把库物化到全新临时目录再读**
（固定目录会跨轮累积出"早已不该存在的题"）。批量删除不要塞进 shell 命令 ——
清理交给 Python 自己做，否则会被安全闸门拦下。

---

## 五、知识空间与流水线

### 三个问题，三条命令

| 想问 | 命令 |
|---|---|
| 有哪些材料、还剩多少点没出题 | `make coverage` · `make coverage-gaps MATERIAL=<材料>` |
| 某个概念该出多少题、还差多少 | `api/.venv/bin/python -m pipeline.graph_log targets` |
| 流水线队列卡在哪、花了多少钱 | `api/.venv/bin/python -m pipeline.status` |

### 一条命令跑完

```bash
make drive     # ingest → dispatch extract → dispatch author → worker → promote → rework
```

也可以分步：`make coverage` 看缺口 → 派工 → `pipeline.worker` 出题（`status=draft`）
→ 独立校验 → `pipeline.promote --apply` 升为 `published`。

**状态机**：`ingest`（切片）→ `extract`（抽点）→ `resolve`（建概念与边）→
`author`（出题，`draft`）→ 校验员（另一会话、重读原文、不看作者解析）→ `pass` 则 `verified`
→ `promote` 成 `published`；`fail` 走 `rework` 重出或退役。

### 失败题必须有归宿

`dead` 与积压的 `draft` 都要有归宿（重出或退役），不许烂在那儿。
`make drive` 已经把 `rework` 含在内。

### 图谱的构建

```bash
make graph          # 归并出概念 + 派共现边 + 统计
make graph-relate   # 让模型给反复共现的概念对判语义关系（花 LLM 的钱，LIMIT= 控规模）
make graph-export   # → graph.json（图谱页取不到接口时的降级快照）
```

图的形状与设计理由见 [THESIS.md](THESIS.md) §6 与 [`pipeline.md`](../.codebuddy/skills/quizforge-author/references/pipeline.md)。

---

## 六、部署

### 环境变量（写在 `.env`）

| 变量 | 说明 |
|---|---|
| `API_PORT` | 宿主端口，默认 `8100` |
| `QF_COOKIE_SECURE` | 上了 HTTPS 后改成 `true` |
| `QF_AI_ENABLED` | 实例级 AI 总开关，设 `false` 后所有人都用不了 |
| `QF_AI_DAILY_QUOTA` | **每人**每日调用上限，`0` 表示不限 |
| `QF_AI_TIMEOUT_MS` | 超时上限（毫秒）|

服务端**不持有、也不提供**任何共享密钥 —— 没有要填的密钥类变量。

### 缓存

资源文件名不带内容哈希，所以缓存策略按"内容会不会变"分档，写在 `api/app/main.py`
的中间件里：字体 `immutable` 长缓存；JS / CSS / HTML 一律 `no-cache`（配合 ETag 走 304）。
不做这件事的后果真实踩过：部署新版本后老用户继续跑缓存里的旧 JS。

### 运维

```bash
make db-down        # 停掉那个搬数据用的 Postgres（搬完就可以停）
make db-dump        # 把快照更新进版本库（db/quizforge.sql.gz）
make db-backup      # 数据文件的备份
```

应用自己不带服务：数据就是 `data/quizforge.db` 一个文件，**备份它就是备份一切**。
打包版（`make package` / `dist-linux`）是单文件，双击即用，不需要 Docker。
代价是**改前端必须重建镜像**（见第一节）。

---

## 七、本机开发

### 常用命令

| 命令 | 作用 |
|---|---|
| `make api-dev` | 后端热重载（`127.0.0.1:8100`）|
| `make web` | 构建前端 → `api/web` |
| `make check` / `test` | 题库校验 / 校验 + 前端自测 |
| `make coverage` / `drive` | 知识空间对账 / 跑一整轮流水线 |
| `make db-dump` / `db-restore` | 快照进版本库 / 恢复 |
| `make bank-export` / `bank-import` | 题目文件形态的导出 / 导入 |
| `make skills-link` | 把 `.codebuddy/skills` 链到工作区根（工作区不是本仓库时用）|
| `make help` | 全部命令 |

### 目录

| 路径 | 是什么 |
|---|---|
| `api/` | FastAPI 后端 + `api/app/cli/`（考纲增删改、导入）|
| `pipeline/` | 出题流水线执行器：ingest / dispatch / worker / promote / rework / coverage / graph_* |
| `theme/` | 前端运行时与页面（`shell.html` 是各页外壳，`runtime/shell.js` 是共享顶栏）|
| `tools/` | 前端构建、题目解析、校验 |
| `db/` | 数据快照 |
| `reference/` · `Codebase/` | 只读材料软链（不在仓库内）|

前端没有打包器：新增运行时脚本要在 `tools/build.py` 与 `tools/build_web.py`
的 `RUNTIME_ORDER` 里都登记，顺序错会引用到未定义的模块。

### 测试

```bash
make test      # 题库校验 + 前端自测（selftest.mjs，唯一用到 Node 的脚本）
make api-test  # pytest
```

改模型后生成迁移：

```bash
make api-migrate              # 先保证基线一致
make api-migration M="说明"    # 再 autogenerate
```

---

## 八、常见问题

**断网能用吗？**
能作答但不能同步：进度先落本机（localStorage），联网后自动补传。断网时打开页面本身会失败
（题库与鉴权都在服务端）。

**手机和电脑的进度会合并吗？**
会，而且不会互相覆盖：作答按**增量**上送，服务端对真正插入成功的流水累加。

**数据存在哪？**
权威在服务端该账号名下（PostgreSQL，容器化时在 docker 卷上）；本机只留一份乐观缓存与
待传队列（浏览器 localStorage）。设置里可导出 / 导入 JSON 备份；整库走 `make db-dump`。

**忘了密码怎么办？**
目前没有找回功能 —— 这是刻意的（未做邮箱验证）。请自行保管密码，
数据可以用 `make db-backup` 备份恢复。

**题目里能放图片吗？**
能写 `![说明](地址)`，但要清楚现在**没有**打包题面图片的路径：构建只产前端与 KaTeX 资源，
服务端也没有图片上传/分发接口。可行的做法：

- **data URI**：`![图](data:image/svg+xml;base64,…)` —— 唯一真正自包含的写法，代价是题面变大
- **绝对网址**：需要联网，且素材在第三方手上

题面里的图多数是公式与代码，优先用 LaTeX 与代码块。结构图建议重绘成 **SVG / mermaid**
（可 diff、可编辑、版权干净），别用文生图模型画结构 —— 它会画错连线，而那正是考点。

> 若确实需要位图（截图、照片），先补一条"图片进仓库 + 服务端可访问"的路径，
> 再在题面里引用 —— 这条还没做。

**字体/排版能改吗？**
前端 CSS 是原生 CSS，没有预处理器；主题色、间距、阴影都是 `theme/app.css` 顶部的 token。
