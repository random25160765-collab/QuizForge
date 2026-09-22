"""数据库引擎与会话。

引擎**惰性创建**：测试里可以先用环境变量换掉 DATABASE_URL 再导入应用，
避免模块级 engine 绑定到错误的库上。

local-first（2026-09-18 起）：默认是**本机数据目录下的一个 SQLite 文件**，
不依赖任何外部服务。SQLite 上有两件事必须手动打开 —— 见 `_configure_sqlite`。
"""

from __future__ import annotations

import json
import logging
import sqlite3
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


def _run(conn, sql: str):  # noqa: ANN001, ANN202
    """同一句 SQL 在**两种连接**上都能跑。

    这里有个来回踩的坑，值得写下来：

    * 走 SQLAlchemy 引擎时，`conn.execute("裸字符串")` 在 2.x 上会抛
      `ObjectNotExecutableError`（要 `text(...)` 或 `exec_driver_sql(...)`）——
      这个错只在**走引擎**那条路上出现，而它跑在**应用启动**时，
      于是打包出来的 exe 连界面都起不来（实测撞过）；
    * 改成 `text(...)` 之后，**文件**那条路（原生 sqlite3 连接）又跑不了了：
      它不认 TextClause，报 TypeError。

    两种连接唯一的交集是**裸字符串**：SQLAlchemy 用 `exec_driver_sql` 收字符串，
    原生 sqlite3 用 `execute` 收字符串。所以统一走字符串。
    """
    runner = getattr(conn, "exec_driver_sql", None)
    return (runner or conn.execute)(sql)


def _rowid_pk_problems(conn) -> list[str]:  # noqa: ANN001
    """SQLite 的自增主键体检：**必须是 `INTEGER PRIMARY KEY`**。

    ## 为什么要专门盯这一条

    SQLite 把 `INTEGER PRIMARY KEY` 当作 rowid 别名，插入时自动给值；
    `BIGINT PRIMARY KEY` 建得出来、也能存，但**不会自增** —— 插入时不给 id 就报
    `NOT NULL constraint failed: <表>.id`。

    我们踩过这个坑，而且踩得很隐蔽：`messages.id` 原先是 `BigInteger`
    （从 Postgres 那边沿袭的），改成 `BigInteger().with_variant(Integer, "sqlite")`
    之后，**新建的库**是对的、**已有的库**还是旧的 —— 而 `create_all` 不会改已有表，
    测试又每次建新库，于是"用例全绿、发出去的包一用就炸"。

    所以这里只做**只读体检**，把话说在前面：真出问题时一眼看得出来。
    （修法是重建库：`python3 tools/migrate_to_local.py`。SQLite 改不了列类型。）
    """
    problems: list[str] = []
    tables = [
        row[0]
        for row in _run(conn, "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    ]
    for table in tables:
        primary = [row for row in _run(conn, f'PRAGMA table_info("{table}")') if row[5]]
        if len(primary) != 1:
            continue  # 复合主键 / 没有主键：不适用这条规则
        name, declared = str(primary[0][1]), str(primary[0][2] or "").upper()
        # 只管**整数**主键；文本主键（questions.id 这类）本来就不该自增
        if "INT" in declared and declared != "INTEGER":
            problems.append(f"{table}.{name} 声明成 {declared}（只有 INTEGER 才能在 SQLite 上自增）")
    return problems


def sqlite_rowid_pk_problems(db_path: Path) -> list[str]:
    """对**库文件**做同一次体检（给打包自检用：它不启动应用，只看文件）。"""
    if not db_path.is_file():
        return []
    with sqlite3.connect(str(db_path)) as conn:
        return _rowid_pk_problems(conn)


#: 「加法列」：模型里有、老库里还没有的那些。
#:
#: 只有**能安全就地补上**的列才配进这张表 —— `ALTER TABLE … ADD COLUMN` 只加一列，
#: 不改已有行的语义（给个默认值即可）。改类型 / 加约束 / 删列都不行，那种事
#: 仍然只能重建库（SQLite 改不了列类型，`_rowid_pk_problems` 那段注释讲了为什么）。
_ADDITIVE_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("conversations", "folder", "VARCHAR(240) NOT NULL DEFAULT ''"),
    # 归档（一层，不分级）：老的对话一律是 False = 未归档，正是想要的默认
    ("conversations", "archived", "BOOLEAN NOT NULL DEFAULT 0"),
    # 置顶：老对话一律 False = 未置顶，正是想要的默认
    ("conversations", "pinned", "BOOLEAN NOT NULL DEFAULT 0"),
    ("messages", "mounts", "TEXT NOT NULL DEFAULT ''"),
    # 资料走到哪一步（检索 / 出题）。默认给"检索"是给**以后新来的行**选的，
    # 老行得靠下面那张回填表 —— 见 `_BACKFILLS`。
    ("materials", "depth", "VARCHAR(16) NOT NULL DEFAULT '检索'"),
    # 「以概念为中心」的判边问过它了吗（`graph_build relate-centric`）。
    # 缺省 NULL = **还没问过**，正是想要的语义（补上之后那些概念会被重新选中，
    # 这是对的：老库里的边本来就少，正该把没问过的补问一遍）。
    ("concepts", "centric_at", "DATETIME"),
)

#: 补完列之后要跑**一次**的回填：`(表, 列) → SQL`，只在那一列**刚补上**时跑。
#:
#: 为什么需要它：`ALTER TABLE ... ADD COLUMN ... DEFAULT x` 会把**已有的行**
#: 也填成 x，而 x 是给"以后新来的行"选的。`materials.depth` 就是例子 ——
#: 默认 `检索` 是对的，但已有的 60 份材料本来就在出题档上，不回填就**全被降级**，
#: 表现是升级之后 `search_material` 突然什么都搜不到（而且不报错）。
_BACKFILLS: dict[tuple[str, str], str] = {
    ("materials", "depth"): "UPDATE materials SET depth = '出题'",
}


def _ensure_columns(conn) -> list[str]:  # noqa: ANN001
    """把缺的加法列补上，返回**补了哪些**（给日志与测试看）。

    为什么要这一道：`create_all` **不改已有的表**。所以在"已经用了一阵"的库上
    加一列，表现是"代码里读得到、库里根本没有" —— 崩在第一次写的时候，
    而测试每次建新库，永远绿。所以应用每次开引擎都顺手补一次。
    """
    added: list[str] = []
    tables = {
        str(row[0])
        for row in _run(conn, "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    }
    for table, column, ddl in _ADDITIVE_COLUMNS:
        if table not in tables:
            continue  # 表还没建（新库）：create_all 会用模型里的完整定义建它
        have = {str(row[1]) for row in _run(conn, f'PRAGMA table_info("{table}")')}
        if column in have:
            continue
        _run(conn, f'ALTER TABLE "{table}" ADD COLUMN "{column}" {ddl}')
        backfill = _BACKFILLS.get((table, column))
        if backfill:
            # 只在**刚补上**这一列时跑：它已经存在就说明回填早做过了
            #（回填是"把老行改对"，不是"每次启动都改一遍"）。
            #
            # ⚠️ 一个已知的例外：**加列与这段代码不是同一次上线**时会漏掉回填。
            # 实测撞过一次 —— 开发时热重载，先存了模型（列建出来了）、后存了这张表
            #（回填才出现），于是列在、回填没跑，60 份材料静静地全被降成了"检索"。
            # 真撞上了就手工补一句（幂等，重复跑无害）：
            #     UPDATE materials SET depth='出题'
            _run(conn, backfill)
        added.append(f"{table}.{column}")
    return added


def ensure_schema(engine: Engine) -> None:
    """把表建出来（幂等），并**当场**报告库结构问题。

    **local-first 之后不再有迁移链**：库就是本机一个文件，第一次运行时它还不存在，
    按模型建表即可 —— 这才是"双击即用"，不该让用户先跑一遍 `alembic upgrade head`。

    建完顺手体检一次（只读）：`create_all` **不会**改已有的表，
    所以"库里是旧结构"这件事必须现在说 —— 否则表现是"过一会儿某个写操作
    报 IntegrityError"（`messages.id` 那次就是这么炸的）。
    """
    from . import models  # noqa: F401, PLC0415 —— 必须先导入模型，metadata 里才有表

    Base.metadata.create_all(engine)

    if engine.dialect.name == "sqlite":
        with engine.begin() as conn:
            # 先补加法列，再体检（补完才谈得上"结构对不对"）
            added = _ensure_columns(conn)
            problems = _rowid_pk_problems(conn)
        if added:
            logging.getLogger("quizforge").info("库结构已就地补上：%s", "、".join(added))
        if problems:
            logging.getLogger("quizforge").warning(
                "库结构是旧的：%s。这意味着写过会失败 —— 重建一份："
                "python3 tools/migrate_to_local.py（SQLite 改不了列类型）",
                "；".join(problems),
            )


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
        # **每次开引擎都过一遍**，不只是"库文件是新建的"那一次。
        # 原先只在 fresh 时跑，后果是：往已有的库里加一张表 / 加一列，代码读得到、
        # 库里根本没有 —— 崩在第一次写的时候。`create_all` 是幂等的（已存在的表跳过），
        # 所以每次跑一遍的代价只是一次 sqlite_master 查询。
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
