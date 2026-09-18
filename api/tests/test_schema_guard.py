"""库结构体检：**自增主键必须是 `INTEGER PRIMARY KEY`**。

这条规则是拿一次真实翻车换来的：`messages.id` 在库里是 `BIGINT`
（从 Postgres 沿袭的写法），SQLite 上不自增 → 内测包发出去，用户发「你好」
当场 `NOT NULL constraint failed: messages.id`。而用例全绿 —— 因为测试每次建新库，
`create_all` 用的是修好的模型，**已有的库**却还是旧结构。

所以这里把判据钉死：整数主键只要不是 `INTEGER` 就算问题。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.db import sqlite_rowid_pk_problems


def _make_db(tmp_path: Path, ddl: str) -> Path:
    path = tmp_path / "t.db"
    with sqlite3.connect(str(path)) as conn:
        conn.execute(ddl)
    return path


def test_bigint_primary_key_is_reported(tmp_path) -> None:  # noqa: ANN001
    """`BIGINT PRIMARY KEY` 建得出来、也能存，但**不会自增** —— 必须被报出来。

    这正是内测包翻车的那张表。
    """
    path = _make_db(tmp_path, "CREATE TABLE messages (id BIGINT PRIMARY KEY, content TEXT)")
    problems = sqlite_rowid_pk_problems(path)
    assert len(problems) == 1
    assert "messages.id" in problems[0]
    assert "BIGINT" in problems[0]


def test_integer_primary_key_is_fine(tmp_path) -> None:  # noqa: ANN001
    """`INTEGER PRIMARY KEY` 是唯一正确的写法（rowid 别名，能自增）。"""
    path = _make_db(tmp_path, "CREATE TABLE messages (id INTEGER PRIMARY KEY, content TEXT)")
    assert sqlite_rowid_pk_problems(path) == []


def test_text_primary_key_is_not_flagged(tmp_path) -> None:  # noqa: ANN001
    """文本主键不该被误报 —— `questions.id` 这类本来就不自增。"""
    path = _make_db(tmp_path, "CREATE TABLE questions (id VARCHAR(64) PRIMARY KEY, stem TEXT)")
    assert sqlite_rowid_pk_problems(path) == []


def test_composite_primary_key_is_not_flagged(tmp_path) -> None:  # noqa: ANN001
    """复合主键不适用这条规则（SQLite 里它们本来就不自增）。"""
    path = _make_db(
        tmp_path,
        "CREATE TABLE edges (a BIGINT NOT NULL, b BIGINT NOT NULL, PRIMARY KEY (a, b))",
    )
    assert sqlite_rowid_pk_problems(path) == []


def test_missing_file_is_not_an_error(tmp_path) -> None:  # noqa: ANN001
    """库文件不在 = 还没建库，不是问题（第一次运行就是这种状态）。"""
    assert sqlite_rowid_pk_problems(tmp_path / "nope.db") == []


def test_engine_path_works_too(tmp_path) -> None:  # noqa: ANN001
    """**走 SQLAlchemy 引擎**的那条路也要能跑。

    这条是补上去的：文件路径（原生 sqlite3）与引擎路径（SQLAlchemy）是两段代码，
    我一开始只在文件路径上测过 —— 结果引擎那条里 `conn.execute("裸字符串")`
    在 SQLAlchemy 2.x 上直接抛 `ObjectNotExecutableError`，
    而它跑在**应用启动**时：打包出来的 exe 连界面都起不来。
    """
    from sqlalchemy import create_engine, text

    from app.db import _rowid_pk_problems  # noqa: PLC0415

    engine = create_engine(f"sqlite:///{tmp_path / 'engine.db'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE messages (id BIGINT PRIMARY KEY, content TEXT)"))
        conn.execute(text("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)"))
        problems = _rowid_pk_problems(conn)
    assert len(problems) == 1
    assert "messages.id" in problems[0]


def test_the_real_database_passes() -> None:
    """**开发库本身**也要过这一关。

    这条用例防的是"体检规则对、但库里真有问题"：开发库是搬运脚本建的，
    如果哪天模型或搬运脚本退化了，这里会先红 —— 而不是等 exe 发出去再炸。
    """
    from app.config import get_settings

    settings = get_settings()
    url = settings.database_url
    if not url.startswith("sqlite"):
        return  # 测试库不是 SQLite 就没什么可查的
    path = Path(url.partition(":///")[2])
    if not path.is_file():
        return  # 还没建库（用例自己会建）
    assert sqlite_rowid_pk_problems(path) == []
