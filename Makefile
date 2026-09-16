# ============================================================================
# quizforge
#
# 两套运行形态：
#   离线单文件（dist/）    全部内联，双击即用，进度在本机 localStorage
#   在线 SaaS（api/web/）  FastAPI + PostgreSQL，账号与进度在服务端
#
#   make all            vendor + check + build（离线产物）
#   make web            构建在线前端到 api/web/
#   make db-up          起 PostgreSQL 容器
#   make api-migrate    执行数据库迁移
#   make api-dev        启动后端（热重载，默认 8100）
#   make api-test       后端测试（pytest）
#   make test           离线侧的题库校验 + 前端逻辑自测
# ============================================================================

PYTHON ?= python3
TOPIC  ?= cpp-stl-iterator
TYPE   ?= single
PORT   ?= 8080
# 宿主 8000 常被别的静态服务器占用，后端默认用 8100
API_PORT ?= 8100
# OUT 是「可对外发布」的目录（不含任何密钥）；
# LOCAL_OUT 是本机自用目录，内含 config/ai.local.json 注入的密钥。
OUT       ?= dist
LOCAL_OUT ?= dist-local
VENV      ?= api/.venv
WEB_OUT   ?= api/web

.PHONY: all vendor check test build build-full build-local split new serve serve-local clean help \
        web api-venv api-dev api-test api-migrate api-migration db-up db-down docker-up docker-down \
        env-init db-backup skills-link

all: vendor check build

help:
	@echo "quizforge 目标："
	@echo "  ---- 离线单文件 ----"
	@echo "  make test              题库校验 + 前端逻辑自测（判分/渲染/SM2/掌握度/持久化）"
	@echo "  make vendor            离线同步 vendor/katex（不联网）"
	@echo "  make check             校验 questions/ 下的题库"
	@echo "  make build             构建可发布的 $(OUT)/{index,quiz,wrongbook}.html（不含密钥）"
	@echo "  make build-local       构建 $(LOCAL_OUT)/，把 config/ai.local.json 的接口与密钥内置"
	@echo "  make split             额外按主题拆分出多个 HTML"
	@echo "  make new TOPIC=cpp-stl-iterator TYPE=single   生成一道新题（TOPIC 取主题树任意一层）"
	@echo "  make serve-local PORT=8080  起本地服务器指向 $(LOCAL_OUT)"
	@echo "  make serve PORT=8080        起本地服务器指向 $(OUT)"
	@echo "  ---- 在线 SaaS ----"
	@echo "  make web               构建在线前端到 $(WEB_OUT)/（页面 + /assets）"
	@echo "  make db-up             起 PostgreSQL 容器（127.0.0.1:5432）"
	@echo "  make api-venv          创建后端虚拟环境并装依赖"
	@echo "  make api-migrate       alembic upgrade head"
	@echo "  make api-dev           启动后端热重载（127.0.0.1:$(API_PORT)）"
	@echo "  make api-test          后端 pytest"
	@echo "  make docker-up         整套（db + api）跑在容器里，宿主 \$${API_PORT:-8100}"
	@echo "  make env-init          生成部署用的 .env（docker compose 会自动读取）"
	@echo "  make db-backup         把数据库导出到 $(HOME)/quizforge-backups/"
	@echo "  make api-migrate-auto M=\"说明\"  按模型改动自动生成迁移"
	@echo "  make clean             清理 $(OUT)/ 与 $(LOCAL_OUT)/"
	@echo "  ---- 出题 skill ----"
	@echo "  make skills-link       把仓库内的 skill 注册到工作区（WORKSPACE=.. 可改）"

vendor:
	@$(PYTHON) tools/vendor.py

check: bank-materialize-all
	@$(BANK_ENV) $(PYTHON) tools/check.py; status=$$?; $(MAKE) -s bank-clean; exit $$status

test: bank-materialize-all
	@$(BANK_ENV) $(PYTHON) tools/check.py
	@$(BANK_ENV) $(PYTHON) tools/build.py -q
	@node tools/selftest.mjs
	@$(MAKE) -s bank-clean

build: bank-materialize
	@$(BANK_ENV) $(PYTHON) tools/build.py --incremental
	@$(MAKE) -s bank-clean

build-full:
	@$(PYTHON) tools/build.py

# 本机自用版本：把 config/ai.local.json 里的接口地址/模型/密钥注入产物，
# 省去每次手填。产物含密钥，切勿分发或部署到公网。
# 刻意输出到 dist-local/：dist/ 是「可以对外发布」的目录，两者不能混。
build-local:
	@test -f config/ai.local.json || { echo "缺少 config/ai.local.json，请先创建"; exit 1; }
	@$(PYTHON) tools/build.py --incremental --ai-config config/ai.local.json --out $(LOCAL_OUT)

split:
	@$(PYTHON) tools/build.py --incremental --split

new:
	@$(PYTHON) tools/new_question.py --topic $(TOPIC) --type $(TYPE)

# 本地迭代用这个：带内置 AI 配置，且不会碰到可发布的 dist/
serve-local: build-local
	@echo "→ http://localhost:$(PORT)/index.html"
	@cd $(CURDIR) && $(PYTHON) -m http.server $(PORT) --directory $(LOCAL_OUT)

serve: build
	@echo "→ http://localhost:$(PORT)/index.html"
	@cd $(CURDIR) && $(PYTHON) -m http.server $(PORT) --directory $(OUT)

clean:
	@rm -rf $(OUT) $(LOCAL_OUT)
	@echo "已清理 $(OUT)/ 与 $(LOCAL_OUT)/"

# ============================================================================
# 在线 SaaS（FastAPI + PostgreSQL）
# ============================================================================

web:
	@$(PYTHON) tools/build.py --web --out $(WEB_OUT)

# 首次准备：创建虚拟环境并装依赖（Python 3.12+）
api-venv:
	@test -x $(VENV)/bin/python || $(PYTHON) -m venv $(VENV)
	@$(VENV)/bin/pip install -q -r api/requirements.txt
	@echo "后端依赖就绪：$(VENV)"

db-up:
	@docker compose up -d db
	@echo "PostgreSQL 已在 127.0.0.1:5432 启动"

db-down:
	@docker compose down

api-migrate:
	@cd api && .venv/bin/alembic upgrade head

# 改完模型后生成迁移；生成前请先 api-migrate 保证基线一致
api-migration:
	@cd api && .venv/bin/alembic revision --autogenerate -m "$(M)"

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

# ---------------------------------------- 题库来自数据库（仓库里不留小文件）
# 权威在 Postgres；下面两个目标把库物化成一个临时目录，构建/校验工具去读它。
BANK_DIR ?= $(CURDIR)/.bank-cache
BANK_ENV  = QF_QUESTIONS_DIR=$(BANK_DIR)/questions QF_TOPICS_FILE=$(BANK_DIR)/meta/topics.yaml

# 校验连草稿一起看（草稿也必须符合契约，只是还不对外发布）
bank-materialize-all:
	@$(VENV)/bin/python -m pipeline.bankfile materialize --out $(BANK_DIR) --status all

# 构建只要已发布的题
bank-materialize:
	@$(VENV)/bin/python -m pipeline.bankfile materialize --out $(BANK_DIR)

# 用完即删：文件只在命令执行期间存在，仓库里不留题库小文件
bank-clean:
	@rm -rf $(BANK_DIR)

# ---------------------------------------------- 数据库快照（必须进版本库）
# 题库、考纲、知识空间的**唯一权威在数据库**；仓库里已经没有它们的小文件，
# 所以数据库必须被推送 —— 否则这份数据只存在于一台机器上。
DB_DUMP ?= db/quizforge.sql.gz
db-dump:
	@mkdir -p db && docker compose exec -T db pg_dump -U $${QF_DB_USER:-quizforge} -d $${QF_DB_NAME:-quizforge} --no-owner --no-privileges | gzip -9 > $(DB_DUMP)
	@ls -lh $(DB_DUMP) | awk '{print "  已导出 " $$9 "（" $$5 "）"}'
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
