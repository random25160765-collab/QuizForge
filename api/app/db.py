"""数据库引擎与会话。

引擎**惰性创建**：测试里可以先用环境变量换掉 DATABASE_URL 再导入应用，
避免模块级 engine 绑定到错误的库上。
"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        settings = get_settings()
        kwargs: dict[str, object] = {
            "echo": settings.db_echo,
            "pool_pre_ping": True,   # 连接被 PG 或中间层掐掉后自动重建
            "future": True,
        }
        if settings.is_postgres:
            kwargs["pool_size"] = settings.db_pool_size
            kwargs["max_overflow"] = settings.db_max_overflow
        _engine = create_engine(settings.database_url, **kwargs)
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
