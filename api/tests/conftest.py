"""pytest 基座。

每个测试会话用一个独立的数据库 ``quizforge_test``：
- 测试之间互不污染，也不会碰到开发库的数据
- 建表走 ``Base.metadata.create_all`` 而不是 alembic —— 迁移脚本本身
  用单独的用例校验（见 test_migrations.py），两条路径各管各的

**注意**：环境变量必须在导入 ``app`` 之前设置好。
``app.config.get_settings`` 带 ``lru_cache``，导入后再改就晚了。
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

API_DIR = Path(__file__).resolve().parent.parent
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))

ADMIN_URL = os.environ.get(
    "QF_TEST_ADMIN_URL", "postgresql+psycopg://quizforge:quizforge@127.0.0.1:5432/quizforge"
)
TEST_DB_NAME = os.environ.get("QF_TEST_DB", "quizforge_test")


def _sync_url(url: str, db_name: str) -> str:
    base, _, _ = url.rpartition("/")
    return f"{base}/{db_name}"


TEST_URL = _sync_url(ADMIN_URL, TEST_DB_NAME)


def _ensure_database() -> None:
    """库不存在就建一个。用 psycopg 直连 admin 库，避免 ORM 的额外抽象。"""
    import psycopg

    dsn = ADMIN_URL.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(dsn, autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DB_NAME,)).fetchone()
        if not exists:
            conn.execute(f'CREATE DATABASE "{TEST_DB_NAME}"')


# 必须在 import app.* 之前落到环境里
os.environ["QF_DATABASE_URL"] = TEST_URL
os.environ["QF_DEBUG"] = "true"
# 实例级 AI 总开关保持打开，好让「每用户自带密钥」那条路径可测。
# 这里不再需要担心误打真实接口：服务端**不持有任何密钥**，
# 密钥只能来自测试自己写进用户设置里的假值。
os.environ.setdefault("QF_AI_ENABLED", "true")


@pytest.fixture(scope="session")
def engine() -> Iterator["object"]:  # noqa: ANN001 - 返回 SQLAlchemy Engine
    _ensure_database()

    # 必须先导入 models，否则 Base.metadata 里一张表都没有，
    # create_all 会静默地什么都不建（alembic/env.py 有同样的坑）
    from app import models  # noqa: F401
    from app.db import Base, get_engine, reset_engine

    reset_engine()
    eng = get_engine()
    Base.metadata.drop_all(eng)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()
    reset_engine()


@pytest.fixture()
def db_session(engine) -> Iterator["object"]:  # noqa: ANN001
    """每个用例一个会话，用例结束回滚，保证互不影响。"""
    from app.db import get_session_factory

    session = get_session_factory()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture()
def client(engine) -> Iterator["object"]:  # noqa: ANN001
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture(scope="session")
def imported_bank(engine) -> "object":  # noqa: ANN001
    """把真实题库导入一次并提交。

    放在 conftest 而不是某个测试模块里：任何需要「库里有题」的用例都要用它，
    而 pytest 的 fixture 只在定义它的模块内可见。

    必须提交 —— 接口层每个请求用独立会话，只能看到已提交的数据。
    """
    from app.bank_import import apply, scan
    from app.db import get_session_factory

    result = scan()
    assert not result.errors, [d.render() for d in result.errors]

    session = get_session_factory()()
    try:
        report = apply(session, result)
        assert report.added, "首次导入应当全部是新增"
        session.commit()
    finally:
        session.close()
    return result
