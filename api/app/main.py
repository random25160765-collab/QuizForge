"""FastAPI 装配。

启动：``api/.venv/bin/uvicorn app.main:app --reload --app-dir api``
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, heavy_deps
from .config import get_settings
from .logging_setup import RequestLogMiddleware, setup_logging
from .routers import all_routers


def create_app() -> FastAPI:
    settings = get_settings()
    logger = setup_logging(settings.debug)

    app = FastAPI(
        title="quizforge API",
        version=__version__,
        docs_url="/api/docs" if settings.debug else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if settings.debug else None,
    )

    if settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_credentials=True,           # 会话 Cookie 必须带这个
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.add_middleware(RequestLogMiddleware, logger=logger)

    for router in all_routers():
        app.include_router(router)

    # ------------------------------------------------------------ 异常兜底
    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        """API 路径一律回 JSON。

        前端 api.js 假定响应体可解析；若这里漏出 HTML 错误页，
        前端拿到的会是一个语焉不详的 JSON 解析失败。
        """
        request_id = getattr(request.state, "request_id", "-")
        logger.exception("[%s] 未处理异常 %s %s", request_id, request.method, request.url.path)
        if request.url.path.startswith("/api/"):
            detail = str(exc) if settings.debug else "服务器内部错误"
            return JSONResponse(status_code=500, content={"detail": detail, "requestId": request_id})
        return JSONResponse(status_code=500, content={"detail": "服务器内部错误"})

    @app.middleware("http")
    async def cache_policy(request: Request, call_next):  # noqa: ANN001, ANN202
        """给静态资源加上明确的缓存策略。

        **不做这件事的后果是真实踩过的**：Starlette 不主动发 `Cache-Control`，
        浏览器于是按 Last-Modified 做启发式缓存。而我们的资源文件名**不带内容哈希**
        （`/assets/runtime/app.js` 永远是同一个 URL），所以部署新版本之后，
        老用户会继续用缓存里的旧 JS —— 表现为「改了前端，刷新看不到」，
        而且会在上线后持续一段时间。

        策略按「内容会不会变」分两档：

        * KaTeX 字体（woff2）：内容永不改变 → 长缓存 + immutable
        * 其余（html / js / css）：文件名固定 → `no-cache`，
          也就是「每次用之前必须回来校验」。配合 ETag 会命中 304，代价很小，
          但保证了部署后立刻生效。
        """
        response = await call_next(request)
        path = request.url.path
        if path.startswith("/assets/fonts/"):
            response.headers.setdefault("Cache-Control", "public, max-age=31536000, immutable")
        elif path.startswith("/assets/") or path.endswith(".html") or path == "/":
            response.headers.setdefault("Cache-Control", "no-cache")
        return response

    @app.middleware("http")
    async def pyodide_cors(request: Request, call_next):  # noqa: ANN001, ANN202
        """只给 `/assets/pyodide/` 开 CORS。

        ## 为什么非开不可

        Python 运行时是在**演示沙箱 iframe** 里加载的，而那个 iframe 刻意不带
        `allow-same-origin`（里面的脚本因此拿不到 cookie 与 DOM）。代价是它的源
        是"不透明"的，请求带上 `Origin: null`；而 Pyodide 会用 `import()` 动态加载
        `pyodide.asm.js`、再用 fetch 取 wasm 与 stdlib —— **模块脚本一律走 CORS**，
        没有这个头就是 `Failed to fetch dynamically imported module`（实测就挂在这）。

        ## 为什么可以开

        那几个文件是公开的运行时文件，不含用户数据、也不需要 cookie，所以 `*` 足够；
        而且**只**放开这一条路径，其他静态资源不受影响。
        """
        response = await call_next(request)
        path = request.url.path
        # pyodide：模块脚本与 fetch 都要 CORS；fonts：**网页字体本身就被 CORS 限制**
        # （沙箱里的 Python 面板是独立文档，它要用我们这份等宽字体）
        if path.startswith("/assets/pyodide/") or path.startswith("/assets/fonts/"):
            response.headers["Access-Control-Allow-Origin"] = "*"
        return response

    # -------------------------------------------------------- 重型组件（Pyodide）
    # 不走 `/assets` 的静态挂载：那份运行时在**用户数据目录**的缓存里
    # （`data/cache/pyodide/`），而且它可能在应用起来**之后**才出现
    # （首次使用或首启下载）—— 挂载点是启动时定死的，跟不上。
    # 所以按文件名逐次取，而且**只认清单里有的名字**（顺带挡住路径穿越）。
    @app.get("/assets/pyodide/{name}", include_in_schema=False)
    def pyodide_asset(name: str) -> FileResponse:
        path = heavy_deps.file_path(name)
        if path is None:
            raise HTTPException(status_code=404, detail="运行时还没准备好")
        return FileResponse(
            path,
            headers={
                # 沙箱是独立文档：模块脚本与 fetch 都要 CORS
                "Access-Control-Allow-Origin": "*",
                # 内容按版本钉死、按哈希校验过，可以放心长缓存
                "Cache-Control": "public, max-age=31536000, immutable",
            },
        )

    # ------------------------------------------------------------ 静态前端
    # 由 `python3 tools/build.py --web` 输出（容器里由 Dockerfile 的多阶段构建生成）。
    # 挂载在最后：FastAPI 按注册顺序匹配，/api/* 必须排在 "/" 之前。
    web_dir = settings.web_dir
    if not web_dir.is_dir():
        web_dir.mkdir(parents=True, exist_ok=True)
        logger.warning("静态目录 %s 不存在，已创建；先跑一次 tools/build.py --web 才会有页面", web_dir)

    assets_dir = web_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")
    app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")

    logger.info(
        "quizforge API 已装配 · debug=%s · db=%s · ai=%s(每用户自带密钥，每人日限 %s) · web=%s",
        settings.debug,
        _safe_db_label(settings.database_url),
        "on" if settings.ai_enabled else "off",
        settings.ai_daily_quota or "不限",
        web_dir,
    )

    if settings.heavy_prefetch:
        # 重型运行时（Pyodide，76M）**第一次打开时取一次**：后台线程去取，
        # 不挡启动、不挡任何请求。已经在缓存里就什么也不做。
        # 测试里关掉（`QF_HEAVY_PREFETCH=false`）—— 用例不该因为跑一次就去联网。
        if heavy_deps.start_prefetch(log=logger.info):
            logger.info("重型运行时不在本机，已在后台开始取（跑 Python 那一项就绪后可用）")

    return app


def _safe_db_label(url: str) -> str:
    """日志里只留库类型与库名，隐去用户名密码。"""
    tail = url.rsplit("@", 1)[-1]
    scheme = url.split("://", 1)[0]
    return f"{scheme}://{tail}"


app = create_app()


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    logging.getLogger("quizforge").info("直接以脚本方式启动，建议用 uvicorn --reload")
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=True)
