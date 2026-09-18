#!/usr/bin/env python3
"""把数据从 Postgres 搬到本机的 SQLite 文件（local-first 的一次性搬运）。

为什么要有这一步：应用从「连库的多用户服务」改成「local-first 单用户应用」，
权威存储也从"起一个 Postgres"变成"数据目录下的一个文件"。库里的东西
（1623 道题、912 个概念、边、作答记录）不能重来一遍 —— 那些是几个月攒的。

## 怎么保证搬对了

* **逐表搬**：按 `Base.metadata.sorted_tables` 的顺序（外键依赖决定的），
  一次一张，整批 insert；
* **搬完对账**：每张表的行数 + 几项关键计数（已发布题、概念、边、记录）。
  任何一项对不上就退出码非零 —— 这个脚本宁可报错也不要"搬了一半"。
* **可重复**：默认只在目标为空时搬；要覆盖得显式 `--force`（先清空再搬）。

用法：

    python3 tools/migrate_to_local.py                       # 从默认 PG 搬到 data/quizforge.db
    python3 tools/migrate_to_local.py --dry-run             # 只报两张表各有多少行
    python3 tools/migrate_to_local.py --from <url> --to <url>
    python3 tools/migrate_to_local.py --force               # 目标已有数据也覆盖
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "api"))

from sqlalchemy import create_engine, delete, func, insert, select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

DEFAULT_FROM = "postgresql+psycopg://quizforge:quizforge@127.0.0.1:5432/quizforge"
DEFAULT_TO = f"sqlite:///{ROOT / 'data' / 'quizforge.db'}"

#: 对账用的关键计数：`(名字, SQL)`。搬完逐条比 —— 行数一样但内容错位是可能的
#: （尤其是 JSON 列与时间列），所以除了 COUNT(*) 还要点几个"有业务含义"的数。
KEY_COUNTS = (
    ("已发布题（published）", "SELECT COUNT(*) FROM questions WHERE status = 'published'"),
    ("现行题（未退役）", "SELECT COUNT(*) FROM questions WHERE retired_at IS NULL"),
    ("材料", "SELECT COUNT(*) FROM materials"),
    ("知识点", "SELECT COUNT(*) FROM knowledge_points"),
    ("概念", "SELECT COUNT(*) FROM concepts"),
    ("概念边", "SELECT COUNT(*) FROM concept_edges"),
    ("题↔概念", "SELECT COUNT(*) FROM question_concepts"),
    ("作答记录", "SELECT COUNT(*) FROM records"),
    ("流水", "SELECT COUNT(*) FROM attempts"),
    ("用户题", "SELECT COUNT(*) FROM user_questions"),
    ("对话", "SELECT COUNT(*) FROM conversations"),
    ("消息", "SELECT COUNT(*) FROM messages"),
)


def _engine(url: str):  # noqa: ANN202
    """建一个一次性引擎。

    **刻意不开外键强制**：整批 insert 时行的先后不受外键摆布（`messages.parent_id`
    是自引用，孩子可能排在父亲前面 —— 实测就撞在这上面）。完整性不靠"边插边查"，
    而是搬完之后用 `PRAGMA foreign_key_check` **一次性证明**（见 `_verify_foreign_keys`），
    这比逐行拦更能说明问题：它查的是整张表的悬空引用。
    """
    kwargs: dict[str, object] = {"future": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        path = url.partition(":///")[2]
        if path and not path.startswith(":memory:"):
            Path(path).parent.mkdir(parents=True, exist_ok=True)
    return create_engine(url, **kwargs)


def _verify_foreign_keys(target) -> list[str]:  # noqa: ANN001
    """搬完之后查一遍悬空引用（SQLite 的 `foreign_key_check`）。

    返回违规清单（空 = 干净）。这是**搬运正确性的关键证据**：
    行数对得上，但引用错位，是这类搬运最典型的翻车方式。
    """
    if target.dialect.name != "sqlite":
        return []
    with target.connect() as conn:
        rows = conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
    return [f"{row[0]} 里有一行指向不存在的 {row[2]}" for row in rows[:20]]


def _tables():  # noqa: ANN202
    """要搬的表（按外键依赖排序）。"""
    from app import models  # noqa: F401  —— 必须先导入，Base.metadata 里才有表
    from app.db import Base

    return list(Base.metadata.sorted_tables)


def _count(engine, sql: str) -> int:  # noqa: ANN001
    with engine.connect() as conn:
        return int(conn.execute(text(sql)).scalar() or 0)


def _insert_all(target, table, rows: list[dict]) -> int:  # noqa: ANN001
    """整批 insert。分块是为了不让一条 SQL 的参数个数爆掉（SQLite 有上限）。"""
    if not rows:
        return 0
    written = 0
    with target.begin() as conn:
        for start in range(0, len(rows), 500):
            chunk = rows[start : start + 500]
            conn.execute(insert(table), chunk)
            written += len(chunk)
    return written


def _prune_users(target, keep_email: str | None) -> list[str]:  # noqa: ANN001
    """把账号收成**本机唯一用户**：留那个有数据的，其余连带数据删掉。

    为什么必须做：库里躺着 17 个账号，其中 16 个是历次验证留下的
    （`probe-…@example.com` 之类），真正的那个是 `random25160765@gmail.com`。
    local-first 只有一个用户，"留哪个"不能靠运气 —— 默认取**消息最多的那个**
    （谁在用一目了然），也可以用 `--keep-user` 指定。

    为什么在**目标库**上做而不是源库：源库那份原样留着 —— 万一搬错了或者删错了，
    源库还在，不用从备份里捞。

    返回被删掉的邮箱清单（打印给人看）。
    """
    from app.models import (  # noqa: PLC0415
        AiUsage,
        Attempt,
        Attachment,
        Conversation,
        DayStat,
        Message,
        Record,
        User,
        UserQuestion,
        UserSettings,
    )

    # 显式按 user_id 删，**不靠外键级联**：搬运时没开外键强制（见 `_engine`），
    # 这里就不该假设它会替我们兜底。顺序：先孩子（attachments / messages），再家长。
    scoped = (
        Attachment,
        Message,
        Conversation,
        Record,
        Attempt,
        DayStat,
        UserSettings,
        AiUsage,
        UserQuestion,
    )

    with Session(target) as session:
        users = list(session.scalars(select(User).order_by(User.created_at)))
        if not users:
            return []
        if keep_email:
            keep = next((u for u in users if u.email == keep_email), None)
            if keep is None:
                raise SystemExit(f"--keep-user {keep_email} 不在库里")
        else:
            # 消息数最多的那个（并列时取更早建的）。
            # 计数走 ORM 而不是拼 SQL 字符串：**UUID 在 SQLite 里是 32 位十六进制**，
            # 拿 `str(uuid)`（带连字符）去比一条也匹配不上 —— 实测踩过，
            # 结果"最活跃用户"全成了 0，挑出的是最早建的验证账号。
            def messages_of(user) -> int:  # noqa: ANN001
                return int(
                    session.scalar(
                        select(func.count()).select_from(Message).where(Message.user_id == user.id)
                    )
                    or 0
                )

            keep = sorted(users, key=lambda u: (-messages_of(u), u.created_at))[0]

        dropped = [u.email for u in users if u.id != keep.id]
        for user in users:
            if user.id == keep.id:
                continue
            for model in scoped:
                session.execute(delete(model).where(model.user_id == user.id))
            session.delete(user)
        session.commit()
        print(f"\n账号收敛：留 {keep.email}（{keep.created_at:%Y-%m-%d}），删掉 {len(dropped)} 个验证残留")

    # 删完确认一遍：级联真的生效了吗（SQLite 上外键默认是关的，这里能兜住配置错误）
    leftovers = (
        _count(target, "SELECT COUNT(*) FROM users"),
        _count(target, "SELECT COUNT(*) FROM messages"),
        _count(target, "SELECT COUNT(*) FROM records"),
    )
    if leftovers[0] != 1:
        raise SystemExit(f"账号没收敛干净：还剩 {leftovers[0]} 个")
    print(f"  账内数据：消息 {leftovers[1]} · 记录 {leftovers[2]}")
    return dropped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tools/migrate_to_local.py", description="PG → SQLite 搬运")
    parser.add_argument("--from", dest="source", default=DEFAULT_FROM, help="源连接串（Postgres）")
    parser.add_argument("--to", dest="target", default=DEFAULT_TO, help="目标连接串（SQLite 文件）")
    parser.add_argument("--force", action="store_true", help="目标已有数据也覆盖（先清空）")
    parser.add_argument("--dry-run", action="store_true", help="只报告，不写")
    parser.add_argument(
        "--keep-user",
        default="",
        help="收成哪一个账号（默认取消息最多的那个）",
    )
    parser.add_argument("--no-prune", action="store_true", help="不收敛账号（原样搬）")
    args = parser.parse_args(argv)

    from app.db import Base  # noqa: PLC0415

    tables = _tables()
    source = _engine(args.source)
    target = _engine(args.target)

    print(f"源  ：{args.source}")
    print(f"目标：{args.target}")
    print(f"表  ：{len(tables)} 张（按外键依赖排序）\n")

    # 目标：建表（SQLite 是空文件，schema 从模型来）
    Base.metadata.create_all(target)

    existing = sum(_count(target, f"SELECT COUNT(*) FROM {t.name}") for t in tables)
    if existing and not args.force:
        print(f"目标库里已经有 {existing} 行数据。要覆盖请加 --force（会先清空目标表）。")
        return 2
    if existing and args.force:
        with target.begin() as conn:
            for table in reversed(tables):        # 逆序删，先删"被依赖的"
                conn.execute(table.delete())
        print(f"已清空目标（原有 {existing} 行）")

    moved: dict[str, int] = {}
    with Session(source) as session:
        for table in tables:
            rows = [dict(row) for row in session.execute(select(table)).mappings()]
            moved[table.name] = len(rows)

    if args.dry_run:
        print(f"{'表':<28}{'源行数':>8}")
        for name, count in moved.items():
            print(f"{name:<28}{count:>8}")
        print(f"\n合计 {sum(moved.values())} 行（--dry-run，没写）")
        return 0

    # 真正搬：一张一张来，边搬边报，出事能一眼看出卡在哪张表
    for table in tables:
        rows = []
        with Session(source) as session:
            rows = [dict(row) for row in session.execute(select(table)).mappings()]
        written = _insert_all(target, table, rows)
        flag = "" if written == moved[table.name] else f"  ← 源是 {moved[table.name]}"
        print(f"  {table.name:<26}{written:>8}{flag}")

    # 对账：行数 + 关键计数
    print("\n对账：")
    bad: list[str] = []
    for table in tables:
        got = _count(target, f"SELECT COUNT(*) FROM {table.name}")
        if got != moved[table.name]:
            bad.append(f"{table.name}：源 {moved[table.name]} ≠ 目标 {got}")
    for label, sql in KEY_COUNTS:
        src, dst = _count(source, sql), _count(target, sql)
        mark = "✓" if src == dst else "✗"
        print(f"  {mark} {label:<22}源 {src:>7} · 目标 {dst:>7}")
        if src != dst:
            bad.append(f"{label}：源 {src} ≠ 目标 {dst}")

    if bad:
        print("\n**对不上**：")
        for item in bad:
            print(f"  ✗ {item}")
        return 1

    dangling = _verify_foreign_keys(target)
    if dangling:
        print("\n**有悬空引用**（外键指向不存在的行）：")
        for item in dangling:
            print(f"  ✗ {item}")
        return 1

    total = sum(moved.values())
    print(f"\n逐表行数与关键计数全部一致（{len(tables)} 张表 · {total} 行）。")
    print("外键完整性：无悬空引用 ✓")

    # 对账通过之后再收敛账号 —— 顺序很重要：先证明**搬得一模一样**，
    # 再做一个"故意不一样"的动作（删掉验证残留），两件事不在一个数里打架。
    if not args.no_prune and not args.dry_run:
        _prune_users(target, args.keep_user or None)

    print(f"\n完成。数据落在：{args.target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
