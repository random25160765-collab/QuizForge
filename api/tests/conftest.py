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

# 测试用的是一组**合成题库夹具**，随代码进版本库（`tests/fixtures/bank/`）。
#
# 为什么不从开发库物化：2026-11-08 起任何用户数据都不进版本库，干净克隆
# 根本没有 `data/quizforge.db`。此前测试依赖本机题库，CI 上只能把整批
# 题库用例跳过 —— 最该在 CI 里跑的那部分，恰恰没跑。夹具很小（5 道题，
# 五种题型各一道），但解析、导入、ETag、统计、选题、诊断这些路径一条不少。
#
# 要拿真实题库跑一遍，显式设 `QF_QUESTIONS_DIR` / `QF_TOPICS_FILE` 即可。
FIXTURE_BANK = API_DIR / "tests" / "fixtures" / "bank"

if not os.environ.get("QF_QUESTIONS_DIR"):
    os.environ["QF_QUESTIONS_DIR"] = str(FIXTURE_BANK / "questions")
if not os.environ.get("QF_TOPICS_FILE"):
    os.environ["QF_TOPICS_FILE"] = str(FIXTURE_BANK / "meta" / "topics.yaml")


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
# 设置文件：**测试永远不碰开发机上那一份**。
#
# `settings_store.put()` 会把整份设置写回文件（2026-11-07 起文件是权威），而这里每个
# 用例又把数据库那一行清掉 —— 不隔离的话，跑一次用例就会把用户真配的 AI / 搜索 /
# 笔记全换成测试里的假值。文件在同一个进程里还是跨用例存在的：前一个用例写下的配置
# 会经 `row()` 的"文件覆盖数据库"漏给后一个用例。所以逐用例换路径
# （见 `_isolate_settings_file`）；这一行是 import 阶段的兜底。
os.environ["QF_SETTINGS_FILE"] = str(
    Path(tempfile.mkdtemp(prefix="qf-test-settings-")) / "settings.local.json"
)
# 不许在测试里去联网取重型运行时（Pyodide 76M）：跑一次用例不该下载几十兆
os.environ["QF_HEAVY_PREFETCH"] = "false"
# 实例级 AI 总开关保持打开，好让「每用户自带密钥」那条路径可测。
# 这里不再需要担心误打真实接口：服务端**不持有任何密钥**，
# 密钥只能来自测试自己写进用户设置里的假值。
os.environ.setdefault("QF_AI_ENABLED", "true")
# 2026-11-08：`QF_AI_BETA_ENABLED` 那行删了 —— 内测通道整块移除之后这个变量没人读，
# 留着只会让人以为"把它打开就能用共享密钥"。


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
    设置、AI 用量本来都挂在 user_id 上，换个用户就等于换个世界（那是账号时代的形状）。
    账号系统整体拆除之后（2026-09-22），隔离就只剩**把这些行清掉**这一件事。

    清的是用户自己的数据；题库与知识空间（`questions` / `concepts` / …）不动 ——
    那些是会话级的 `imported_bank` 灌进来的，用例之间本来就该共享。

    **这份名单不再写在这里**：原先这里手写着 9 张表，而它已经漂过一次 ——
    `conversation_folders` 没在里面，那个表于是在用例之间不清（谁先跑谁留下分组）。
    现在从 `app.datapack.USER_TABLES` 取 —— 那是"哪张表属于用户"的唯一事实来源，
    与数据包、导出导入共用同一份声明。
    """
    from sqlalchemy import text

    from app import models  # noqa: F401 —— 让 Base.metadata 里表都在（排序要用到）
    from app.datapack import USER_TABLES
    from app.db import Base, get_engine

    # **先删孩子再删父母**。SQLite 的外键是真在的：`schedule_events.item_id`
    # 指着 `plan_items`，按名字字典序清就会先删 `plan_items` 而撞上约束
    # （实测：用例里一旦留下"排好的块"，下一条用例的隔离就红在这里）。
    # `sorted_tables` 是父在前，反过来就是孩子在前。
    order = [one.name for one in reversed(Base.metadata.sorted_tables) if one.name in set(USER_TABLES)]

    with get_engine().begin() as conn:
        for table in order:
            conn.execute(text(f'DELETE FROM "{table}"'))
    yield


@pytest.fixture(autouse=True)
def _isolate_settings_file(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """每个用例一份**空的**设置文件（理由见文件顶部 `QF_SETTINGS_FILE` 那段）。"""
    monkeypatch.setenv("QF_SETTINGS_FILE", str(tmp_path / "settings.local.json"))


@pytest.fixture(autouse=True)
def _runtime_always_ready(monkeypatch) -> None:  # noqa: ANN001
    """把「Pyodide 运行时已就绪」钉成前提。

    运行时（144M）不进安装包，首次由 `heavy_deps` 取进本机缓存；而上面那句
    `QF_HEAVY_PREFETCH=false` 又把"顺手取一份"关掉了（跑一次用例不该下几十兆）。
    于是 `tools.pyodide_base()` 在干净检出和 CI 上都返回 `None`，`run_python`
    走"运行时还没到位"那条早返回 —— **那是对的**，它不该假装能跑。

    但好几条用例验的是**零件的形状**（只带 runId、样板在常驻壳里、壳里有
    `loadPackagesFromImports` 与 Agg 后端），跟运行时在不在无关。不钉住这一项，
    它们测的就成了"跑它的机器有没有那份缓存"，而且**不稳**：某条用例触发后台
    预取之后缓存补齐，后面的用例又过了 —— 实测 CI 上 3 条里只红 1 条就是这个。

    钉的只是 `tools.pyodide_base` 这一处；`heavy_deps.is_ready()` 本身不动，
    它有自己的用例（`test_heavy_deps.py`，显式传目录进去）。
    """
    from app import tools

    monkeypatch.setattr(tools, "pyodide_base", lambda: "__ORIGIN__/assets/pyodide/")


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
    """把合成题库夹具导入一次并提交，**并发布**。

    放在 conftest 而不是某个测试模块里：任何需要「库里有题」的用例都要用它，
    而 pytest 的 fixture 只在定义它的模块内可见。

    必须提交 —— 接口层每个请求用独立会话，只能看到已提交的数据。

    为什么还要显式发布一次：导入器刻意只产出 `draft`（发布是流水线 `promote`
    的职责），而 `/api/bank` 只放行 verified / published。少了这一步，库里有
    题、接口却回空数组 —— 相关用例会以"接口没题"的形式失败，很容易
    被误读成接口坏了。
    """
    from sqlalchemy import update

    from app.bank_import import apply, scan
    from app.db import get_session_factory
    from app.models import Question

    result = scan()
    assert not result.errors, [d.render() for d in result.errors]
    assert result.questions, f"没有找到题库夹具：{FIXTURE_BANK}"

    session = get_session_factory()()
    try:
        report = apply(session, result)
        assert report.added, "首次导入应当全部是新增"
        session.execute(update(Question).where(Question.retired_at.is_(None)).values(status="published"))
        session.commit()
    finally:
        session.close()
    return result
