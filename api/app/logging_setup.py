"""日志配置。

保持简单但可观测：每次请求一行（方法 / 路径 / 状态 / 耗时 / 用户），
敏感信息（密码、token、AI 密钥）一律不进日志。
"""

from __future__ import annotations

import logging
import sys
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

LOGGER_NAME = "quizforge"


def setup_logging(debug: bool = False) -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    if logger.handlers:                      # 避免 --reload 下重复添加
        return logger
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s", datefmt="%H:%M:%S")
    )
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    logger.propagate = False
    return logger


class RequestLogMiddleware(BaseHTTPMiddleware):
    """给每个请求打一个 id 并记录耗时。

    request_id 会回写响应头，方便把浏览器侧的一次报错和服务器日志对上。
    """

    def __init__(self, app, logger: logging.Logger) -> None:
        super().__init__(app)
        self.logger = logger

    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[no-untyped-def]
        request_id = uuid.uuid4().hex[:12]
        request.state.request_id = request_id
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            elapsed = (time.perf_counter() - started) * 1000
            self.logger.exception(
                "[%s] %s %s 500 (%.0fms) 未捕获异常", request_id, request.method, request.url.path, elapsed
            )
            raise
        elapsed = (time.perf_counter() - started) * 1000
        response.headers["X-Request-Id"] = request_id

        # 静态资源不记流水，否则日志会被字体文件刷满
        if not request.url.path.startswith(("/assets/", "/favicon")):
            level = logging.WARNING if response.status_code >= 400 else logging.INFO
            self.logger.log(
                level,
                "[%s] %s %s %d (%.0fms)",
                request_id,
                request.method,
                request.url.path,
                response.status_code,
                elapsed,
            )
        return response
