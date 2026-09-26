# ============================================================================
# quizforge
#
# **local-first**：数据落在 `data/quizforge.db`（SQLite，随应用分发），
# 不依赖任何外部服务 —— `make api-test` / `make test` 都不必先起数据库。
# 前端是普通静态资源，由本机的 FastAPI 进程提供。
#
#   make web            构建前端到 api/web/
#   make api-dev        启动后端（热重载，默认 8100）
#   make api-test       后端测试（pytest，跑在临时 SQLite 文件上）
#   make check          题库校验（从库物化后校验，0 error 是硬线）
#   make test           上面的校验 + 前端逻辑自测
#   make graph-check    图谱体检（无环 / 能走到根 / 闭包不矛盾）
#
# 数据快照（**唯一进版本库的数据形态**，就是 SQLite 库本身）—— 分两份：
#   make db-snapshot   一次写两个：
#                        db/quizforge.db.gz               不含向量 → **提交它**
#                        db/quizforge.db.with-vectors.gz  含向量   → 本机自己留（已 gitignore）
#   make db-restore    db/quizforge.db.gz → data/quizforge.db（解压即用；向量靠 make embed 重建）
#                      要连向量一起搬：make db-restore ARGS="--path db/quizforge.db.with-vectors.gz"
# ============================================================================

PYTHON ?= python3
TOPIC  ?= cpp-stl-iterator
TYPE   ?= single
# 宿主 8000 常被别的静态服务器占用，后端默认用 8100
API_PORT ?= 8100
VENV      ?= api/.venv
WEB_OUT   ?= api/web

# 跑 `tools/` 下那些**校验类**脚本时用哪个 python —— **venv 里那个**。
#
# 为什么不是 `$(PYTHON)`（系统 python3）：`tools/check.py` 经 `tools/topics.py`
# 要 `import yaml`，而系统 python **不保证**装了 PyYAML（开发机上碰巧有，
# CI 里那个干净的 3.12 就没有 —— 实测第一次跑 `make check` 就红在这儿）。
# 这条链路本来就已经依赖 venv（物化题目走 `$(VENV)/bin/python`），
# 所以统一用它：只要跑过 `make api-venv`（README 的第一步）就一定能用。
CHECK_PYTHON = $(VENV)/bin/python

.PHONY: vendor check test new web web-watch web-full package pyodide-manifest notes-import \
        win-setup win-sync dist dist-release dist-linux dist-linux-release smoke \
        api-venv api-dev api-test dev \
        env-init db-snapshot db-restore \
        bank-export bank-import graph graph-check graph-relate graph-relate-centric \
        graph-export share skills-link embed intake \
        coverage coverage-gaps drive help cases

vendor:
	@$(PYTHON) tools/vendor.py

# check / test 在文件末尾附近定义 —— 它们都要先"从库物化到临时目录"，
# 定义在一起才看得清那一串前置条件（见「题库来自数据库」一节）。

new:
	@$(PYTHON) tools/new_question.py --topic $(TOPIC) --type $(TYPE)

# ============================================================================
# 前端（FastAPI + PostgreSQL，唯一形态）
# ============================================================================

web:
	@$(PYTHON) tools/build_web.py --out $(WEB_OUT)

# 图表引擎的回归套件（见 tools/texlab/run_cases.js 的说明）。
# 规矩：**每加一处兜底，必须在这里带一条用例**；没有用例的改写一律不加。
# 需要两个服务在跑：仓库根（8199，用例文件在那儿）与 应用（8100）。
cases: web
	@cp tools/texlab/cases.json $(WEB_OUT)/cases.json
	@curl -s -o /dev/null --max-time 3 http://127.0.0.1:8100/cases.json || \
	  { echo "  先起应用服务（8100，见 make dev）"; exit 1; }
	@playwright-cli close >/dev/null 2>&1 || true
	@playwright-cli open http://127.0.0.1:8100/chat.html --browser=chromium >/dev/null 2>&1
	@playwright-cli run-code "$$(cat tools/texlab/run_cases.js)" 2>&1 | sed -n '2p' | $(PYTHON) tools/texlab/report.py

# 单独挂 watcher（`make dev` 已经带了；只想盯前端时用这个）
web-watch:
	@$(VENV)/bin/python tools/watch_theme.py

# 把 76M 的 Pyodide 也拷进前端产物：**离线自包含**用（产物从 ~5M 变 ~81M）。
# 分发包不要这个 —— 运行时改由首启取进本机缓存（见 build/package.py）。
web-full:
	@$(PYTHON) tools/build_web.py --out $(WEB_OUT) --with-pyodide

# ============================================================================
# 笔记库导入
#
# 把旧编辑器的笔记库搬进应用数据目录（`data/notes/<库名>/`）。三件事同时成立：
# **源目录只读** · **第二次跑不产生副本** · **你改过的笔记不会被冲掉**（报冲突、不动）。
#
#   make notes-import VAULT=Math        导入一个库
#   make notes-import                   根目录下所有库
#   make notes-import VAULT=Math DRY=1  只看会做什么，一个字都不写
# ============================================================================

notes-import:
	@$(VENV)/bin/python tools/import_vault.py $(if $(VAULT),--vault $(VAULT),) $(if $(DRY),--dry-run,)

# 单文件可执行程序（不含重型组件）。**必须在本平台构建**：PyInstaller 不能交叉编译。
# 这条出的是"当前平台"的包 —— 在 WSL 里跑它得到的是 Linux 二进制，
# 只用来验证打包链本身。要发出去的包走 `make dist`（见下）。
package:
	@$(VENV)/bin/python build/package.py build

# ============================================================================
# 分发工作流
#
# **唯一事实是 WSL 侧这份仓库。** Windows 侧那个目录（`%USERPROFILE%\qf-build\src`）
# 只是构建用的**镜像**，不许手改 —— 改了会在下次同步时被覆盖。
# 打包必须在 Windows 上跑（PyInstaller 不能交叉编译），所以是"这边开发、那边出包"。
#
#   make win-setup     首次准备 Windows 侧构建环境（Python + venv + 依赖，幂等）
#   make win-sync      把仓库镜像过去（删掉多出来的、逐文件校验哈希）
#   make dist          出**内测**包（默认通道）：带走内测配置，试用的人不用自备密钥
#   make dist-release  出正式包：不带内测配置，且数据目录与内测分开
#
# 日常顺序：`win-setup` 一次 → 之后每次改完代码直接 `make dist`（它会先构建前端、
# 再同步、再在 Windows 上打包并自检、最后把产物放到桌面与 build/dist/）。
# ============================================================================

win-setup:
	@$(VENV)/bin/python build/setup_win.py

win-sync:
	@$(VENV)/bin/python build/sync_win.py

# 发版前跑一次：对**真 exe** 做一次真实对话烟测（起包 → 发一句 → 关掉）。
# 会走一次模型（花钱），所以不挂进 dist —— 但它抓的是"能启动、能读、不能写"这类
# 只在真跑时才暴露的问题（内测包第一版就死在 messages.id 上）。
smoke: win-sync
	@PYTHONUNBUFFERED=1 $(VENV)/bin/python build/smoke_win.py

# 界面检查：六页冒烟 + 笔记编辑器（源码/编辑两个视图、⌘Z、⌘S 只差一行、装饰是否生效）。
# 需要先起着服务（默认 http://127.0.0.1:8100），并且本机能 require 到 playwright-core：
#     QF_PLAYWRIGHT=/path/to/playwright-core make ui-check
ui-check:
	@QF_BASE=$${QF_BASE:-http://127.0.0.1:8100} node tools/checks/notes_editor.mjs

dist:
	@$(MAKE) --no-print-directory web
	@# PYTHONUNBUFFERED：这一步要跑两三分钟，输出被缓冲的话看起来像卡死
	@PYTHONUNBUFFERED=1 $(VENV)/bin/python build/dist_win.py --channel beta

dist-release:
	@$(MAKE) --no-print-directory web
	@PYTHONUNBUFFERED=1 $(VENV)/bin/python build/dist_win.py --channel release

# Linux 的包：在**本机**就能出（开发机就是 Linux），不用绕 Windows。
# 产物是 `build/dist/quizforge-beta`（内测）或 `quizforge`（正式），自检自动跑。
dist-linux:
	@$(MAKE) --no-print-directory web
	@PYTHONUNBUFFERED=1 $(VENV)/bin/python build/package.py build --channel beta

dist-linux-release:
	@$(MAKE) --no-print-directory web
	@PYTHONUNBUFFERED=1 $(VENV)/bin/python build/package.py build --channel release

# 从 vendor/pyodide 生成首启下载时用的哈希清单（`make vendor` 之后跑，产物要提交）
pyodide-manifest:
	@$(VENV)/bin/python build/package.py manifest

# 首次准备：创建虚拟环境并装依赖（Python 3.12+）
api-venv:
	@test -x $(VENV)/bin/python || $(PYTHON) -m venv $(VENV)
	@$(VENV)/bin/pip install -q -r api/requirements.txt
	@echo "后端依赖就绪：$(VENV)"

# ---- 数据快照：**唯一进版本库的数据形态**（SQLite 库本身，不是某种导出版本）----
#
# 权威是 `data/quizforge.db`（本机一个文件）；快照用来"换台机器接着干"。
# 这里刻意**不再有 Postgres**：
#   * 快照就是 SQLite 库本身，解压即用，没有"先起一台库再灌 dump"这一步；
#   * 载入即校验（完整性 + 外键悬空），坏快照当场拒绝，不会悄悄给出一个半库。
#
# 快照**必须抹掉 AI 密钥**再落盘：它是进版本库的（仓库公开），而 `app_settings`
# 里存着用户自己填的密钥。踩过：2026-09-17 导出的快照里带过一把真密钥。
#
# **分两份**（2026-09-26）：向量占全库 82.9%，而它是派生物、又**压不动**
# （float32 的 blob 走 zlib 只有 1.08 倍，正文有 3.8 倍）—— 进了版本库就是按原始
# 大小长、历史里还删不掉。所以进 git 的那份**不含向量**，带向量的那份本机自己留：
# 换机器带上它就不用重算，不带也行（`make embed` 重建）。详见 tools/db_snapshot.py。
DB_SNAPSHOT ?= db/quizforge.db.gz
DB_VECTORS  ?= db/quizforge.db.with-vectors.gz

# 库里 → 两份快照。用 SQLite 的 backup API 取一致性快照（有 WAL 也不会读到半写状态）。
db-snapshot:
	@$(VENV)/bin/python tools/db_snapshot.py save --out $(DB_SNAPSHOT) --no-vectors
	@$(VENV)/bin/python tools/db_snapshot.py save --out $(DB_VECTORS)

# 快照 → 库里（`--force` 覆盖已有的库）。这是**换新机器/首次上手**那条路。
# 这份没有向量：`make embed` 补齐（GPU 约 10 分钟、CPU 一小时多）。
# 要连向量一起搬：make db-restore ARGS="--path $(DB_VECTORS)"
db-restore:
	@$(VENV)/bin/python tools/db_snapshot.py load --path $(DB_SNAPSHOT) $(ARGS)

# ---------------------------------------------------------------- 数据库运维（只读）
# 这些以前是"每次开个 python -c 现场写"：库多大、哪个表占地方、某个 slug 在不在。
# 写一次扔一次的代价不是打字，是**结论留不下来** —— 下次还得再量一遍，而量法稍有
# 不同就会得出不同的数。所以固化成 tools/db_ops.py（只读，改不到你的库）。
db-size:
	@$(VENV)/bin/python tools/db_ops.py size

db-counts:
	@$(VENV)/bin/python tools/db_ops.py counts

db-query:
	@test -n "$(Q)" || { echo '需要 Q="一句 SQL"（只读）'; exit 1; }
	@$(VENV)/bin/python tools/db_ops.py query "$(Q)"

.PHONY: db-size db-counts db-query

# 起开发服务前**必构建前端**：前端是静态产物，不构建看到的就是上一次的样子 ——
# 实测踩过（"开发端找不到笔记入口"，其实是产物还是旧的）。构建戳会打出来，
# 和 exe 上的对一下就知道是不是同一份（见 `make dist-check`）。
# 监听 0.0.0.0 而不是 127.0.0.1：仓库在 WSL 里、浏览器在 Windows 上，
# 而 WSL2 的 localhost 转发在这台机器上不通（实测：从 Windows 取 127.0.0.1:8100 连不上）。
# 0.0.0.0 在 WSL 里只是 vNIC（NAT），只有 Windows 宿主机够得到，不会挂到局域网上。
api-dev: web
	@echo "→ 打开 http://127.0.0.1:$(API_PORT)/chat.html  （对话是前台；其余页面走左侧活动栏）"
	@ip=$$(hostname -I 2>/dev/null | tr " " "\n" | grep -E "^[0-9]" | grep -v "^169\.254\." | head -1); \
	 if [ -n "$$ip" ]; then echo "  Windows / 局域网：http://$$ip:$(API_PORT)/chat.html"; fi
	@echo "  前台阻塞（Ctrl+C 停）。要它在后台跑：nohup make api-dev >/tmp/quizforge.log 2>&1 &"
	@cd api && .venv/bin/uvicorn app.main:app --reload --host 0.0.0.0 --port $(API_PORT)

# 桌面那个包是不是**当前这份代码**打的：比对它随附的构建戳与现在 `api/web` 的戳。
# 这条是给"开发端改了、exe 还是老的"这种情况准备的 —— 一句话就能问清楚。
dist-check:
	@$(VENV)/bin/python build/dist_win.py --check

# 开发通道：本机起 web 服务（**不是**分发形态）。
# 它用的是仓库里的 `data/`（不是 exe 那两份 `~/quizforge*`），改代码即时生效。
#
# 前端是静态产物：**改 `theme/` 不重建，浏览器看到的还是上一版**（为此踩过不止一次）。
# 所以这里把 `web-watch` 挂在后台 —— 改一下就自动重建，不需要多一步 `make web`。
dev: web
	@$(VENV)/bin/python tools/watch_theme.py &
	@$(MAKE) api-dev

api-test:
	@cd api && .venv/bin/python -m pytest

# ---------------------------------------------------------------------- 资料运维
# 资料（向量化）的进度。**前台看**，刷新一屏、带进度条，没活了自己退出。
#
#   make watch      实时刷新（Ctrl-C 退出；后台的活不受影响）
#   make ops        只画一帧（脚本与 agent 用这个）
#   make ops-json   同一份数据，给程序读
#
# 为什么做成快捷键：这一点上反复敲命令 = 反复写一次性脚本，而 `pipeline/ops.py`
# 就是为了把那件事固化下来（它同时也负责调度、对账与跑单账本）。
watch:
	@$(VENV)/bin/python -m pipeline.ops watch

ops:
	@$(VENV)/bin/python -m pipeline.ops

ops-json:
	@$(VENV)/bin/python -m pipeline.ops --json

.PHONY: watch ops ops-json

# ---------------------------------------------------------------------- GPU
# 装了 N 卡的那台机器上跑一次。它只做两件事：装依赖、把"这台机器该用什么"写进
# `config/embed.local.json` —— CUDA 那几个包 1.4G，不该写进所有人的 requirements。
#
#   make gpu-check   只体检（卡在不在、provider 有没有、现在会用哪个变体）
#   make gpu-setup   装依赖 + 写本机配置（有卡就用 fp16 走 GPU）
#   make gpu-fp16    顺带取 fp16 模型（1.1G，清单里有 sha256 校验）
gpu-check:
	@scripts/gpu-setup.sh --check

gpu-setup:
	@scripts/gpu-setup.sh

gpu-fp16:
	@scripts/gpu-setup.sh --with-fp16

.PHONY: gpu-check gpu-setup gpu-fp16

# ---------------------------------------------------------------------- 一键补充向量
# 往 Windows 那边（F:\Books 之类）丢一个新文件 → `make add` → 前端就能查到。
#
# 它做四件事，一件都不少：**扫目录找新文件**（正文抽过没有，是唯一判据）→ **抽正文**
# （PDF 走 pdftotext / docx 走 pandoc，见 api/app/library.py）→ **登记 + 切片** → **算向量**。
# 三者的幂等性都是现成的：抽正文按修改时间增量、登记按 sha256 判"已最新"、
# 向量只算缺的与正文变了的 —— 所以**重复跑不会重算**，第二次跑就是几秒的"没有新东西"。
#
#   make add                      收 SRC 这个目录（默认 /mnt/f/Books）
#   make add SRC=/mnt/f/Website   收别的目录（一次一个）
#   make add SUBJECT=cuda         新条目归到哪个学科（默认按路径猜）
#   make add DRY=1                只说要收哪些，一个字都不写
#
# 收进来的东西一律落在**检索**档（`materials.depth`，拿来查、不进出题链）；
# 哪几篇值得精读出题，在资料库里把它们提到 `出题`，那是另一条路（见 pipeline/intake.py）。
SRC     ?= /mnt/f/Books
SUBJECT ?=
add:
	@$(VENV)/bin/python -m pipeline.intake --from $(SRC) \
		$(if $(SUBJECT),--subject $(SUBJECT),) $(if $(WORKERS),--workers $(WORKERS),) $(if $(DRY),--dry-run,)
	@$(VENV)/bin/python -m pipeline.embed $(if $(EMBED_ARGS),$(EMBED_ARGS),)
	@echo "  好了，前端可以直接查：http://127.0.0.1:$(API_PORT)/chat.html"

.PHONY: add

# ---------------------------------------------------------------------- 图片进检索
# 材料里的图，**图里的字**用本地 OCR、**图的内容**让模型说一句，都写回正文 —— 只有
# 写回正文，图才进得了切片与向量（否则问"这张图画了什么"是搜不到的：实测那批网页
# 材料 148 条图片引用，只有 34% 带 alt，大半还是"图片"两个字）。
#
# 写进去的行首标着来路（`图里的字（OCR）：` / `图说明（模型读图）：`）—— 机器从图里
# 读出来的与原生正文不是一个可信度，引用落到它上面时要看得出来。重跑先删后写，不叠加。
#
#   make images                    所有带图的材料
#   make images MATERIAL=<slug>    只做一份
#   make images DRY=1              只列要抓/要读哪些图，一个字不写、不联网
#   make images OCR=1              只要 OCR（本地、免费；不花钱读图）
#   make images FORCE=1            处理过的图也重来（默认跳过：读图按张收费）
#   make images WORKERS=8          读图并发（默认 8）
#   make images OCR_WORKERS=2      OCR 并发（默认 4）。**走 GPU 时显存吃得凶**：
#                                  4 路实测 7.8/8.1 GB —— 要和 `make embed` 同时跑就把
#                                  这里降到 2；`QF_OCR_CUDA=0` 则强制回 CPU（OCR 默认走卡）。
#
# 图会抓到 data/library/.text/.media/<slug>/（CDN 那种本机没有副本的），抓过不重抓。
# 之后自动重切 + 增量算向量：**只重算正文变了的材料**。
# 默认**只做"检索"层**（资料库：网页、书）。出题层那 16 份材料有它们自己的读图支路，
# 而重切它们的正文会动到出题账本认的切片 —— 要一起做加 ALL=1（或点名 MATERIAL=<slug>）。
IMAGES_ARGS = $(if $(MATERIAL),--material $(MATERIAL),) $(if $(DRY),--dry-run,) \
              $(if $(OCR),--no-caption,) $(if $(ALL),--all-depths,) $(if $(FORCE),--force,) \
              $(if $(WORKERS),--workers $(WORKERS),) $(if $(OCR_WORKERS),--ocr-workers $(OCR_WORKERS),)

images:
	@# PYTHONUNBUFFERED：这一步要跑十几分钟（抓图 + OCR + 逐张读图），输出被缓冲的
	@# 话，后台跑起来看着像卡死 —— 与 `dist` 那条目标同一个理由。
	@PYTHONUNBUFFERED=1 $(VENV)/bin/python -m pipeline.images $(IMAGES_ARGS)
	@$(VENV)/bin/python -m pipeline.intake $(if $(MATERIAL),--citekey $(MATERIAL),)
	@$(VENV)/bin/python -m pipeline.embed $(if $(MATERIAL),--material $(MATERIAL),)
	@echo "  好了，图里的内容现在和正文一样可检索：http://127.0.0.1:$(API_PORT)/chat.html"

.PHONY: images

# 整套跑在容器里（含构建镜像与迁移）
# docker-up / docker-down 已删（2026-09-20）：它们起的是「api 容器」那套部署，
# 而应用现在是本机单文件（PyInstaller）或本机 uvicorn —— api/Dockerfile 也一起删了。
# Postgres 那一套（db-up / db-restore / db-dump）也在 2026-09-22 全部删掉：
# 换引擎的搬运**已经做完了**，快照现在就是 SQLite 库本身（见 `db-snapshot`），
# 仓库里不再有任何需要 Docker 或 Postgres 的路径。

# 生成 .env（本机跑服务时读它）
# 已有 .env 时拒绝覆盖 —— 里面可能有按机器调整过的端口等设置。
# 注意这里**没有密钥要填**：AI 密钥在 `config/ai.local.json`（已 gitignore），
# 那是本机自己的那一份，不随仓库走。
env-init:
	@test -f .env && { echo ".env 已存在，未覆盖"; exit 0; } || cp .env.example .env
	@echo "已生成 .env（按需修改端口 / 用量上限 / 内测通道开关）"

# ============================================================================
# 出题 skill
# ============================================================================

# 把仓库内的 skill 注册到工作区（默认仓库的上一级）。
#
# skill 的**实体就在仓库里**（见 .codebuddy/skills/README.md），这里只建指针 ——
# 换工作区、或工作区的 .codebuddy/ 被清了，重跑一次即可。
# 用 ln -sfn 而不是 rm + ln：目标已是软链就替换，目标若是真实目录则拒绝覆盖并报错，
# 不会把别人放在那里的东西删掉。
WORKSPACE ?= ..
skills-link:
	@mkdir -p $(WORKSPACE)/.codebuddy/skills
	@for d in .codebuddy/skills/quizforge-*; do \
		[ -d "$$d" ] || continue; \
		name=$$(basename "$$d"); \
		rel=$$(realpath --relative-to=$(WORKSPACE)/.codebuddy/skills "$$d"); \
		ln -sfn "$$rel" "$(WORKSPACE)/.codebuddy/skills/$$name" || exit 1; \
		echo "link  $$name  →  $$rel"; \
	done
	@echo "完成：实体仍在本仓库 .codebuddy/skills/，工作区里只是指针"

# ------------------------------------------------- 题库交换（库 ↔ 单一文件）
# 数据库是唯一权威；文件只在「对外部署」与「审阅/备份」时出现，且只有一个文件。
bank-export:
	@$(VENV)/bin/python -m pipeline.bankfile export --out $(CURDIR)/bank.json

bank-import:
	@$(VENV)/bin/python -m pipeline.bankfile import --path $(CURDIR)/bank.json

# ---------------------------------------------------------------- 知识图谱
# merge 把 981 个点归并成概念（幂等，可反复跑）；edges 派共现边；
# relate 让模型给反复共现的概念对判语义关系（花 LLM 的钱，按 --limit 控制规模）。
graph:
	@$(VENV)/bin/python -m pipeline.graph_build merge
	@$(VENV)/bin/python -m pipeline.graph_build edges
	@$(VENV)/bin/python -m pipeline.graph_build stats
	@$(VENV)/bin/python -m pipeline.graph_build check

# 图谱体检：无环 / 能走到根 / 闭包不矛盾。硬不变量破了退出码非零，可直接当闸门。
# 补边的进度看它报的 `unrooted`（一条有序边都没有的概念数）。
graph-check:
	@$(VENV)/bin/python -m pipeline.graph_build check $(ARGS)

graph-relate:
	@$(VENV)/bin/python -m pipeline.graph_build relate --limit $(or $(LIMIT),400)

# 以概念为中心判前置：一次问一个概念 + 它的候选。盯着**还没接进有序边**的概念问，
# 那是"路径推荐断在半路"的地方。问过就记 `centric_at`，重跑不重复买。
graph-relate-centric:
	@$(VENV)/bin/python -m pipeline.graph_build relate-centric --limit $(or $(LIMIT),20)

graph-export:
	@$(VENV)/bin/python -m pipeline.graph_build export --out $(CURDIR)/graph.json

# 把一条会话打成**一个能发出去的网页**（对话正文 + 那棵对话树，`file://` 双击即用）。
#   make share                # 最近更新的那一条
#   make share CID=<uuid>     # 指定会话
# 产物落在仓库根 `share-<id8>.html`。它**只读**、不需要后端 —— 装配进去的是构建好的
# 页面本身（渲染器只有一份源码），为什么这样可以内联、以及装配时踩过的坑，
# 见 tools/share.py 的文件头。
share:
	@$(VENV)/bin/python tools/share.py $(or $(CID),latest) $(ARGS)

# 窗口向量化 —— 检索层的那一路（"换个说法问同一件事"靠它，见 docs/检索与向量化.md）。
# 模型是**本地的小模型**（bge-m3 int8，约 600MB）：首次跑时自动下载到本机缓存，
# 之后离线可用、零边际成本。**不进分发包** —— 那是"首启下载"，见 api/app/local_embed.py。
# **派生产物，随时可以重跑**：默认只算缺的与正文变了的；换了模型用 ARGS="--rebuild"。
# 只把模型取到本机（不向量化）：`make embed ARGS="--fetch"`。
# 先看要算什么而不动库：`make embed ARGS="--dry-run"`（这条连模型都不用）。
embed:
	@$(VENV)/bin/python -m pipeline.embed $(ARGS)

# ---------------------------------------- 题库来自数据库（仓库里不留小文件）
# 权威在 Postgres；构建与校验都先把库物化到一个**全新的临时目录**再读它。
#
# 为什么是"全新临时目录"而不是一个固定的缓存目录：物化是"只写不清"的，
# 固定目录会**跨轮次累积** —— 同一道题在改过 topic 之后会以两个文件名共存，
# 于是 `check` 报"id 重复"，产物里混进早已不该存在的题（实测踩过：
# 导出的题库停在两千多道旧题的版本上，后端对账测试因此莫名失败）。
# 每次换目录，就不存在"上一轮的残渣"这个问题。
#
# 清理交给 Python（shutil.rmtree），不走 shell 的 rm：临时目录里有两千多个文件，
# 走 shell 会被"批量删除需要确认"拦下，而这是流水线自己的中间产物，不该打扰人。
BANK_MATERIALIZE = $(VENV)/bin/python -m pipeline.bankfile materialize

# 校验连草稿一起看（草稿也必须符合契约，只是还不对外发布）。
# **先扫密钥再校题**：前者一秒就跑完，且一旦命中就该立刻停下 ——
# 密钥只要推出去了，后面所有校验都变得没有意义（收不回来，只能换）。
check:
	@$(CHECK_PYTHON) tools/secret_scan.py
	@BANK=$$(mktemp -d "$${TMPDIR:-/tmp}/qf-bank-XXXXXX"); \
	$(BANK_MATERIALIZE) --out $$BANK --status all >/dev/null; \
	QF_QUESTIONS_DIR=$$BANK/questions QF_TOPICS_FILE=$$BANK/meta/topics.yaml $(CHECK_PYTHON) tools/check.py; \
	status=$$?; $(CHECK_PYTHON) -c "import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)" $$BANK; \
	exit $$status

test:
	@# 先过一遍语法：运行时脚本只要有一处解析失败，整页就白屏
	@# （实测踩过 —— chat.js 被编辑截断，逻辑断言一条都抓不到）
	@for f in theme/runtime/*.js; do node --check "$$f" || exit 1; done
	@BANK=$$(mktemp -d "$${TMPDIR:-/tmp}/qf-bank-XXXXXX"); \
	PUB=$$(mktemp -d "$${TMPDIR:-/tmp}/qf-bank-XXXXXX"); \
	$(BANK_MATERIALIZE) --out $$BANK --status all >/dev/null; \
	QF_QUESTIONS_DIR=$$BANK/questions QF_TOPICS_FILE=$$BANK/meta/topics.yaml $(CHECK_PYTHON) tools/check.py; \
	status=$$?; \
	if [ $$status -eq 0 ]; then \
	  $(BANK_MATERIALIZE) --out $$PUB >/dev/null; \
	  (cd api && QF_QUESTIONS_DIR=$$PUB/questions QF_TOPICS_FILE=$$PUB/meta/topics.yaml \
	     .venv/bin/python -m app.cli.import_bank --dataset-out $$PUB/bank.json >/dev/null); \
	  status=$$?; \
	fi; \
	if [ $$status -eq 0 ]; then QF_BANK_JSON=$$PUB/bank.json node tools/selftest.mjs; status=$$?; fi; \
	$(CHECK_PYTHON) -c "import shutil,sys; [shutil.rmtree(p, ignore_errors=True) for p in sys.argv[1:]]" $$BANK $$PUB; \
	exit $$status

# 资料库入库：登记 + 切片，**止步于此**（不抽点 / 不归概念 / 不出题 —— 见 pipeline/intake.py）。
# 之后接 `make embed` 就有向量了。逐条确认要不要继续往下走看 `materials.depth`。
intake:
	@$(VENV)/bin/python -m pipeline.intake $(ARGS)

# 覆盖率对账：哪些材料出过题、还剩多少点（与派工的"只补缺口"同一套判据）
coverage:
	@$(VENV)/bin/python -m pipeline.coverage $(ARGS)

coverage-gaps:
	@$(VENV)/bin/python -m pipeline.coverage --material $(MATERIAL) --gaps

# 状态机自动跑：出题 → 校验 → 发布 → 打回重出，收敛即停
drive:
	@$(VENV)/bin/python -m pipeline.drive $(ARGS)

# ---------------------------------------------------------------- 帮助
# 新 session 先看这个：目标按用途分组，流水线那几条是最常用的。
help:
	@echo "quizforge · 常用目标"
	@echo ""
	@echo "  上手        make api-venv · make api-dev（http://127.0.0.1:8100）"
	@echo "  补充向量    make add（收 /mnt/f/Books 里的新文件 → 抽正文 → 切片 → 向量）"
	@echo "              make add SRC=/mnt/f/Website SUBJECT=cuda · make add DRY=1 先看会做什么"
	@echo "  流水线      make coverage · make coverage-gaps MATERIAL=x · make drive [ARGS=...]"
	@echo "              四步：dispatch → worker → promote --apply → rework --apply（drive 已含）"
	@echo "  校验        make check（密钥扫描 + 题库校验）· make test · make graph-check"
	@echo "  构建发布    make web（前端 → api/web/）· make package / dist-linux（单文件）"
	@echo "  数据快照    make db-restore（快照 → data/quizforge.db，解压即用）"
	@echo "              make db-snapshot（库里 → 快照，提交它）· 另见 bank-export / bank-import"
	@echo "  其它        make env-init（生成 .env）· make skills-link · make help"
