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
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

API_DIR = Path(__file__).resolve().parent.parent
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))

# 测试库：一个一次性的 SQLite 文件。
#
# local-first 之后**开发与运行同一套引擎**（见 `app/db.py`），测试也跑在同一套上 ——
# 换库那一步的验收就是这 180 多项用例：它们把「JSON 怎么存」「upsert 怎么写」
# 「时间戳怎么比」这些方言差别全踩一遍。测试不再需要"先把数据库起起来"。
TEST_DB = Path(os.environ.get("QF_TEST_TMP", tempfile.gettempdir())) / f"quizforge-test-{os.getpid()}.db"
TEST_URL = f"sqlite:///{TEST_DB}"

# 物化题库要读**权威库**（开发库），不是测试库。
BANK_SOURCE_URL = os.environ.get(
    "QF_BANK_SOURCE_URL",
    f"sqlite:///{Path(__file__).resolve().parents[2] / 'data' / 'quizforge.db'}",
)


def _ensure_bank_source() -> None:
    """题库的权威在数据库，测试也从库里取。

    `app.bank_import` 是按**目录**写的（那条路径是给不接数据库的老部署留的），
    所以这里先把库物化到临时目录，再把 `QF_QUESTIONS_DIR` 指过去 ——
    仓库里不再需要留一份 `questions/`。必须在导入 `app` 之前跑完
    （`get_settings` 带 lru_cache，导入后再改环境变量就晚了）。
    """
    if os.environ.get("QF_QUESTIONS_DIR"):
        return
    repo_questions = API_DIR.parent / "questions"
    if repo_questions.is_dir() and any(repo_questions.rglob("*.md")):
        os.environ["QF_QUESTIONS_DIR"] = str(repo_questions)
        return

    import subprocess
    import tempfile

    bank_dir = Path(tempfile.mkdtemp(prefix="qf-test-bank-"))
    proc = subprocess.run(
        [sys.executable, "-m", "pipeline.bankfile", "materialize", "--out", str(bank_dir)],
        cwd=str(API_DIR.parent),
        env={**os.environ, "QF_DATABASE_URL": BANK_SOURCE_URL},
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        print("[conftest] 物化题库失败：", proc.stdout[-400:], proc.stderr[-400:])
        return
    os.environ["QF_QUESTIONS_DIR"] = str(bank_dir / "questions")
    os.environ["QF_TOPICS_FILE"] = str(bank_dir / "meta" / "topics.yaml")


_ensure_bank_source()


def _ensure_database() -> None:
    """**重建**测试库：文件存在就先删掉（连 WAL 的附属文件一起）。

    为什么每次都重建而不是"不存在才建"：导入只增不删（缺的内容置 retired_at），
    于是上一次跑留下的行会让断言取决于"这台机器之前跑过什么" ——
    实测就撞过：题库里的凑数学科删掉之后，旧测试库里的考纲分组还在，
    「入库再读出来 == 导入时的数据集」这条断言于是挂在分组上，
    看起来像产品 bug，其实是残留。测试库本来就是一次性的。
    """
    for suffix in ("", "-wal", "-shm"):
        path = Path(f"{TEST_DB}{suffix}")
        if path.exists():
            path.unlink()


# 必须在 import app.* 之前落到环境里
os.environ["QF_DATABASE_URL"] = TEST_URL
os.environ["QF_DEBUG"] = "true"
# 不许在测试里去联网取重型运行时（Pyodide 76M）：跑一次用例不该下载几十兆
os.environ["QF_HEAVY_PREFETCH"] = "false"
# 实例级 AI 总开关保持打开，好让「每用户自带密钥」那条路径可测。
# 这里不再需要担心误打真实接口：服务端**不持有任何密钥**，
# 密钥只能来自测试自己写进用户设置里的假值。
os.environ.setdefault("QF_AI_ENABLED", "true")
# 内测通道默认**关**：那些「没密钥就该被拦住」的用例测的正是这条契约，
# 开着它会把 503 变成"连不上内测通道"的 502，测试就测的是别的东西了。
# 通道本身的行为由 test_beta_channel.py 单独打开来测。
os.environ.setdefault("QF_AI_BETA_ENABLED", "false")


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


@pytest.fixture(autouse=True)
def _isolate_user_data(engine) -> Iterator[None]:  # noqa: ANN001
    """每个用例前把**属于本机用户的数据**清干净。

    原先的隔离是"每个用例注册一个新用户"顺带给的：进度、对话、附件、题单、
    设置、AI 用量都挂在 user_id 上，换个用户就等于换个世界。
    单用户本地形态下没有第二个用户（见 `app/deps.py`），所以隔离得显式做 ——
    **在同一个用户下把这些行清掉**。

    清的是用户自己的数据；题库与知识空间（`questions` / `concepts` / …）不动 ——
    那些是会话级的 `imported_bank` 灌进来的，用例之间本来就该共享。
    """
    from sqlalchemy import text

    from app.db import get_engine

    tables = (
        "attachments",
        "messages",
        "conversations",
        "records",
        "attempts",
        "days",
        "user_settings",
        "ai_usage",
        "user_questions",
    )
    with get_engine().begin() as conn:
        for table in tables:
            conn.execute(text(f"DELETE FROM {table}"))
    yield


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


def local_user_id() -> str:
    """本机用户的 id —— 单用户本地形态下就是那个唯一的用户。

    原先测试靠 `POST /api/auth/register` 拿用户（顺带拿到会话与 CSRF），
    账号面删掉之后（见 `app/deps.py`）就只剩这一件事要做：
    把本机用户准备好、拿到它的 id。

    自己开一个会话并提交：接口层每个请求用独立会话，只能看到已提交的数据。
    """
    from sqlalchemy.orm import Session

    from app.db import get_engine
    from app.deps import get_local_user

    with Session(get_engine()) as session:
        return str(get_local_user(session).id)


@pytest.fixture()
def local_user(engine) -> Iterator["object"]:  # noqa: ANN001
    """本机用户对象（要直接读它字段的用例用这个）。"""
    from sqlalchemy.orm import Session

    from app.db import get_engine
    from app.deps import get_local_user

    session = Session(get_engine())
    try:
        yield get_local_user(session)
    finally:
        session.close()


@pytest.fixture(scope="session")
def imported_bank(engine) -> "object":  # noqa: ANN001
    """把真实题库导入一次并提交，**并发布**。

    放在 conftest 而不是某个测试模块里：任何需要「库里有题」的用例都要用它，
    而 pytest 的 fixture 只在定义它的模块内可见。

    必须提交 —— 接口层每个请求用独立会话，只能看到已提交的数据。

    为什么还要显式发布一次：导入器刻意只产出 `draft`（发布是流水线 `promote`
    的职责），而 `/api/bank` 只放行 verified / published。少了这一步，库里有
    1657 道题、接口却回空数组 —— 相关用例会以"接口没题"的形式失败，很容易
    被误读成接口坏了。
    """
    from sqlalchemy import update

    from app.bank_import import apply, scan
    from app.db import get_session_factory
    from app.models import Question

    result = scan()
    assert not result.errors, [d.render() for d in result.errors]

    session = get_session_factory()()
    try:
        report = apply(session, result)
        assert report.added, "首次导入应当全部是新增"
        session.execute(update(Question).where(Question.retired_at.is_(None)).values(status="published"))
        session.commit()
    finally:
        session.close()
    return result
