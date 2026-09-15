# quizforge 后端镜像
#
# 构建上下文是**仓库根**（导入器要复用 tools/ 与 meta/、questions/）：
#     docker build -f api/Dockerfile -t quizforge-api .
# 或直接用 docker compose。

# ============================================================================
# 阶段 1：在镜像内生成前端静态产物
#
# 这样镜像**自包含** —— 部署时不依赖宿主先跑 `make web`。
# 之前靠 `./api/web:/app/web` 绑定挂载提供前端，而 api/web 在 .gitignore 里，
# 一旦在干净的机器上部署，挂载目录是空的，页面直接白屏。
#
# 只拷 theme/ 与 vendor/：`--web` 不需要题库（题库走接口），
# 所以改题目不会让这一层缓存失效。
# ============================================================================
FROM python:3.12-slim AS web

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# 前端构建只用到标准库 + PyYAML
RUN pip install --no-cache-dir PyYAML

WORKDIR /src
COPY tools /src/tools
COPY theme /src/theme
COPY vendor /src/vendor

RUN python tools/build.py --web --out /src/web


# ============================================================================
# 阶段 2：运行时镜像
# ============================================================================
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 依赖单独一层：改代码不会导致重装依赖
COPY api/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# 后端
COPY api/app /app/app
COPY api/alembic /app/alembic
COPY api/alembic.ini /app/alembic.ini

# 题库导入器复用仓库里已有的解析器与考纲。
# 容器里 api/ 与 tools/ 是平级的，config.py 靠 QF_TOOLS_DIR 找到它们
# （仓库布局下默认就是 <root>/tools，不需要设置）。
COPY tools /app/tools
COPY meta /app/meta
COPY questions /app/questions

ENV QF_TOOLS_DIR=/app/tools

# 前端产物来自阶段 1，而不是宿主目录
COPY --from=web /src/web /app/web

# 以非 root 运行，容器逃逸时损失小一些
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
