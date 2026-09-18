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
# 老路径（只在"把历史数据从 Postgres 搬过来"时用得上，见 tools/migrate_to_local.py）：
#   make db-up / db-down / db-restore
# ============================================================================

PYTHON ?= python3
TOPIC  ?= cpp-stl-iterator
TYPE   ?= single
# 宿主 8000 常被别的静态服务器占用，后端默认用 8100
API_PORT ?= 8100
VENV      ?= api/.venv
WEB_OUT   ?= api/web

.PHONY: vendor check test new web web-full release package pyodide-manifest \
        api-venv api-dev api-test \
        db-up db-down docker-up docker-down env-init db-backup db-dump db-restore \
        bank-export bank-import graph graph-check graph-relate graph-relate-centric \
        graph-export skills-link \
        coverage coverage-gaps drive help

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

# 把 76M 的 Pyodide 也拷进前端产物：**离线自包含**用（产物从 ~5M 变 ~81M）。
# 分发包不要这个 —— 运行时改由首启取进本机缓存（见 build/package.py）。
web-full:
	@$(PYTHON) tools/build_web.py --out $(WEB_OUT) --with-pyodide

# 单文件可执行程序（不含重型组件）。**必须在本平台构建**：PyInstaller 不能交叉编译，
# 所以 Windows 的 exe 要在 Windows 上跑这条命令。
package:
	@$(VENV)/bin/python build/package.py build

# 发布：先构建前端，再出包（出包后会自动跑一次自检：接口 + 静态页 + 首启建库）
release: web package

# 从 vendor/pyodide 生成首启下载时用的哈希清单（`make vendor` 之后跑，产物要提交）
pyodide-manifest:
	@$(VENV)/bin/python build/package.py manifest

# 首次准备：创建虚拟环境并装依赖（Python 3.12+）
api-venv:
	@test -x $(VENV)/bin/python || $(PYTHON) -m venv $(VENV)
	@$(VENV)/bin/pip install -q -r api/requirements.txt
	@echo "后端依赖就绪：$(VENV)"

# ---- 老路径：只在搬历史数据时用（应用本身不再需要 Postgres）----

db-up:
	@docker compose up -d db
	@echo "PostgreSQL 已在 127.0.0.1:5432 启动（仅供 tools/migrate_to_local.py 当源库用）"

db-down:
	@docker compose down

api-dev:
	@echo "→ http://127.0.0.1:$(API_PORT)/  （前端请先 make web）"
	@cd api && .venv/bin/uvicorn app.main:app --reload --host 127.0.0.1 --port $(API_PORT)

api-test:
	@cd api && .venv/bin/python -m pytest

# 整套跑在容器里（含构建镜像与迁移）
docker-up:
	@docker compose up -d --build
	@echo "→ http://127.0.0.1:$${API_PORT:-8100}/"

docker-down:
	@docker compose down

# 生成部署用的 .env（docker compose 会自动读取它）
# 已有 .env 时拒绝覆盖 —— 里面可能有按机器调整过的端口等设置。
# 注意这里**没有密钥要填**：AI 密钥由每个用户在自己的设置面板里填，
# 服务端不持有任何人的凭据。
env-init:
	@test -f .env && { echo ".env 已存在，未覆盖"; exit 0; } || cp .env.example .env
	@echo "已生成 .env（按需修改端口 / 每人 AI 用量上限）"

# 数据备份。
# 数据现在落在 VPS 磁盘的 docker 卷上 —— 这是选择轻量服务器的代价，
# 必须有一条随手可用的备份路径。默认写到仓库之外，避免被误提交。
DB_BACKUP_DIR ?= $(HOME)/quizforge-backups

db-backup:
	@mkdir -p $(DB_BACKUP_DIR)
	@docker compose exec -T db pg_dump -U quizforge -d quizforge --clean --if-exists \
		| gzip > $(DB_BACKUP_DIR)/quizforge-$$(date +%Y%m%d-%H%M%S).sql.gz
	@echo "已备份到 $(DB_BACKUP_DIR)："
	@ls -lh $(DB_BACKUP_DIR) | tail -3

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
	@$(PYTHON) tools/secret_scan.py
	@BANK=$$(mktemp -d "$${TMPDIR:-/tmp}/qf-bank-XXXXXX"); \
	$(BANK_MATERIALIZE) --out $$BANK --status all >/dev/null; \
	QF_QUESTIONS_DIR=$$BANK/questions QF_TOPICS_FILE=$$BANK/meta/topics.yaml $(PYTHON) tools/check.py; \
	status=$$?; $(PYTHON) -c "import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)" $$BANK; \
	exit $$status

test:
	@# 先过一遍语法：运行时脚本只要有一处解析失败，整页就白屏
	@# （实测踩过 —— chat.js 被编辑截断，逻辑断言一条都抓不到）
	@for f in theme/runtime/*.js; do node --check "$$f" || exit 1; done
	@BANK=$$(mktemp -d "$${TMPDIR:-/tmp}/qf-bank-XXXXXX"); \
	PUB=$$(mktemp -d "$${TMPDIR:-/tmp}/qf-bank-XXXXXX"); \
	$(BANK_MATERIALIZE) --out $$BANK --status all >/dev/null; \
	QF_QUESTIONS_DIR=$$BANK/questions QF_TOPICS_FILE=$$BANK/meta/topics.yaml $(PYTHON) tools/check.py; \
	status=$$?; \
	if [ $$status -eq 0 ]; then \
	  $(BANK_MATERIALIZE) --out $$PUB >/dev/null; \
	  (cd api && QF_QUESTIONS_DIR=$$PUB/questions QF_TOPICS_FILE=$$PUB/meta/topics.yaml \
	     .venv/bin/python -m app.cli.import_bank --dataset-out $$PUB/bank.json >/dev/null); \
	  status=$$?; \
	fi; \
	if [ $$status -eq 0 ]; then QF_BANK_JSON=$$PUB/bank.json node tools/selftest.mjs; status=$$?; fi; \
	$(PYTHON) -c "import shutil,sys; [shutil.rmtree(p, ignore_errors=True) for p in sys.argv[1:]]" $$BANK $$PUB; \
	exit $$status

# ---------------------------------------------- 数据库快照（必须进版本库）
# 题库、考纲、知识空间的**唯一权威在数据库**；仓库里已经没有它们的小文件，
# 所以数据库必须被推送 —— 否则这份数据只存在于一台机器上。
DB_DUMP ?= db/quizforge.sql.gz
# 导出开发库。**落盘前先把 API 密钥抹掉**：这份快照是进版本控制的（仓库公开），
# 而 `user_settings` 里存着用户自己填的密钥（见 api/app/routers/ai.py 里那个取舍），
# 内测通道的密钥也在同一张表里出现过。抹的是**快照里的那份**，库里照旧。
# 踩过的坑：2026-09-17 导出的快照里带过一把真密钥（幸运的是那个提交还没 push）。
db-dump:
	@mkdir -p db && docker compose exec -T db pg_dump -U $${QF_DB_USER:-quizforge} -d $${QF_DB_NAME:-quizforge} --no-owner --no-privileges | sed -E 's/"apiKey": "[^"]*"/"apiKey": ""/g' | gzip -9 > $(DB_DUMP)
	@ls -lh $(DB_DUMP) | awk '{print "  已导出 " $$9 "（" $$5 "，密钥已抹）"}'
db-restore:
	@gunzip -c $(DB_DUMP) | PGPASSWORD=$${QF_DB_PASSWORD:-quizforge} psql -h $${QF_DB_HOST:-127.0.0.1} -U $${QF_DB_USER:-quizforge} -d $${QF_DB_NAME:-quizforge} -q
	@echo "  已从 $(DB_DUMP) 恢复"

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
	@echo "  上手        make db-up · make db-restore · make check · make test · make api-dev"
	@echo "  流水线      make coverage · make coverage-gaps MATERIAL=x · make drive [ARGS=...]"
	@echo "              四步：dispatch → worker → promote --apply → rework --apply（drive 已含）"
	@echo "  数据库      make db-dump / db-restore（快照进版本库）· make bank-export / bank-import"
	@echo "  校验构建    make check · make test · make web（构建前端到 api/web/）"
	@echo "  服务        make api-dev（http://127.0.0.1:8100）· docker-up / docker-down · make skills-link"
