"""数据库引擎与会话。

引擎**惰性创建**：测试里可以先用环境变量换掉 DATABASE_URL 再导入应用，
避免模块级 engine 绑定到错误的库上。

local-first（2026-09-18 起）：默认是**本机数据目录下的一个 SQLite 文件**，
不依赖任何外部服务。SQLite 上有两件事必须手动打开 —— 见 `_configure_sqlite`。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


def as_json(value: Any, default: Any = None) -> Any:
    """把**裸 SQL** 读到的 JSON 列解成 Python 对象。

    为什么需要这一道：`text()` 写的查询没有类型信息，方言不会替我们解码 ——
    Postgres 的 jsonb 驱动直接给 dict/list，SQLite 的 TEXT 给的是原样的**字符串**。
    换内置引擎之后，所有"裸 SQL 读 JSON 列"的地方都要过它一遍，否则会得到
    `'str' object has no attribute 'get'` 这种莫名其妙的错（实测撞在物化题库上）。

    已经是 dict/list 就原样返回 —— 同一段代码在两种方言下都对。
    """
    if value is None:
        return default
    if isinstance(value, (dict, list, bool, int, float)):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "replace")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return default


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def _sqlite_path(url: str) -> Path | None:
    """从 `sqlite:///…` 里取出文件路径（内存库返回 None）。"""
    if not url.startswith("sqlite"):
        return None
    _, _, tail = url.partition(":///")
    if not tail or tail.startswith(":memory:"):
        return None
    return Path(tail)


def _configure_sqlite(engine: Engine) -> None:
    """SQLite 上必须手动打开的两件事 —— 默认都是关的。

    * **外键**：默认 `foreign_keys=OFF`，于是 `ondelete="CASCADE"` 全是摆设
      （删一条材料不会带走它的点）。单用户本地更要开，因为回滚靠它兜底。
    * **WAL**：读写可以并行。本地单用户也会一边跑出题流水线、一边点界面，
      默认的 rollback journal 会让两者互相阻塞到像卡死。
    """

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn, _record) -> None:  # noqa: ANN001
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()


def ensure_schema(engine: Engine) -> None:
    """把表建出来（幂等）。

    **local-first 之后不再有迁移链**：库就是本机一个文件，第一次运行时它还不存在，
    按模型建表即可 —— 这才是"双击即用"，不该让用户先跑一遍 `alembic upgrade head`。

    真要改结构时（比如给已有用户的库加一列），在这里补一段按版本号判断的逻辑，
    版本号写在 `meta_documents`（`schema_version`）里 —— 表不用新加。
    """
    from . import models  # noqa: F401, PLC0415 —— 必须先导入模型，metadata 里才有表

    Base.metadata.create_all(engine)


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        settings = get_settings()
        url = settings.database_url
        kwargs: dict[str, object] = {"echo": settings.db_echo, "future": True}

        path = _sqlite_path(url)
        fresh = path is not None and not path.exists()
        if path is not None:
            # 第一次运行时数据目录还不存在 —— 由应用自己建，别让用户先 mkdir
            path.parent.mkdir(parents=True, exist_ok=True)
            # 一个文件被多个线程共用（FastAPI 的线程池 + 后台流水线），必须放行
            kwargs["connect_args"] = {"check_same_thread": False}
        else:
            # 老路径（Postgres）：连接被中间层掐掉后自动重建
            kwargs["pool_pre_ping"] = True

        _engine = create_engine(url, **kwargs)
        if path is not None:
            _configure_sqlite(_engine)
        if fresh:
            # 库文件是刚建出来的 —— 顺手把表建好（只在第一次发生）
            ensure_schema(_engine)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=get_engine(),
            autoflush=False,
            autocommit=False,
            # 提交后不失效对象：接口层常需要在 commit 之后继续读字段组装响应
            expire_on_commit=False,
        )
    return _session_factory


def reset_engine() -> None:
    """测试用：丢弃已缓存的引擎（换库时调用）。"""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


def get_db() -> Iterator[Session]:
    """FastAPI 依赖：每个请求一个会话，异常时回滚。"""
    session = get_session_factory()()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
