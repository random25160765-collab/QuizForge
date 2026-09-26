#!/usr/bin/env python3
"""数据库本身的运维：**看它多大、里面有什么、随手查一句**。

## 为什么要固化成一个工具

这些事以前是"每次开一个 `python -c` 现场写"：库多大、哪个表占地方、向量那一列到底
多少字节、某个 slug 在不在。写一次扔一次的代价不是打字，是**结论留不下来** ——
同一个数下次还得再量一遍，而量法稍有不同就会得出不同的数（这次就因为漏看一个
`typeof` 把列名猜成 `vector` 而白跑一轮）。

所以：**只读**、**不写库**、结论按固定格式打出来。写库的事归别的工具
（快照归 `tools/db_snapshot.py`，向量归 `pipeline/embed.py`）。

## 用法

    python3 tools/db_ops.py size            # 体积构成：哪个表占地方、向量怎么存的
    python3 tools/db_ops.py counts          # 行数一览（材料 / 题 / 图谱）
    python3 tools/db_ops.py query "SELECT slug, depth FROM materials LIMIT 5"
    python3 tools/db_ops.py size --json     # 给程序吃

全部走 `mode=ro` 打开：这个工具**不可能**改到你的库。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "quizforge.db"

#: `size` 里那张"每千行正文多少向量"的换算要用到 —— 它是**实测**出来的，
#: 不是估的（2026-09-26：603370 行正文、36320 个窗口、141.9 MB 向量）。
LINE_SAMPLES = ((100 * 10000, "再收 100 本 × 1 万行"), (100 * 100000, "再收 100 本 × 10 万行"))


def connect(db: Path) -> sqlite3.Connection:
    """只读连接。库不在就给一句人话，而不是一个 traceback。"""
    if not db.is_file():
        print(f"没有库：{db}", file=sys.stderr)
        raise SystemExit(1)
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def table(row) -> str:  # noqa: ANN001
    """一行结果 → 一行文本（None 打成空，省得看见一片 `None`）。"""
    return " | ".join("" if value is None else str(value) for value in row)


def describe(db: Path) -> dict:
    """体积构成。返回的是**数**，打印是另一回事（`--json` 要能直接给程序）。"""
    conn = connect(db)
    try:
        cur = conn.cursor()
        page_size = cur.execute("PRAGMA page_size").fetchone()[0]
        page_count = cur.execute("PRAGMA page_count").fetchone()[0]
        info: dict = {
            "file": str(db),
            "bytes": db.stat().st_size,
            "pageSize": page_size,
            "pageCount": page_count,
            "freelist": cur.execute("PRAGMA freelist_count").fetchone()[0],
            "tables": [],
        }
        # 各表真实占用要问 dbstat（SQLite 编进来才有）。没有就退到"按行数 + 平均行长"，
        # 那个数偏大但方向对 —— 一并说明来源，免得和 dbstat 的数混着比。
        try:
            rows = cur.execute(
                "SELECT name, SUM(pgsize) AS bytes FROM dbstat GROUP BY name ORDER BY bytes DESC"
            ).fetchall()
            total = sum(int(size) for _name, size in rows) or 1
            info["tables"] = [
                {"name": name, "bytes": int(size), "share": int(size) / total}
                for name, size in rows
            ]
            info["tableBytesSource"] = "dbstat"
        except sqlite3.OperationalError as exc:
            info["tableBytesSource"] = f"没有 dbstat（{exc}）"

        try:
            kind, rows_n, avg, mx = cur.execute(
                "SELECT typeof(vec), COUNT(*), AVG(LENGTH(vec)), MAX(LENGTH(vec))"
                " FROM slice_embeddings GROUP BY typeof(vec)"
            ).fetchone()
            info["vectors"] = {"storedAs": kind, "count": int(rows_n), "avgBytes": float(avg),
                               "maxBytes": int(mx), "netBytes": int(float(avg) * rows_n)}
        except sqlite3.OperationalError:
            info["vectors"] = None

        try:
            info["models"] = [
                {"model": model, "dim": int(dim), "count": int(count)}
                for model, dim, count in cur.execute(
                    "SELECT model, dim, COUNT(*) FROM slice_embeddings GROUP BY model, dim"
                )
            ]
        except sqlite3.OperationalError:
            info["models"] = []

        scalars = {}
        for name, sql in (
            ("materials", "SELECT COUNT(*) FROM materials"),
            ("slices", "SELECT COUNT(*) FROM material_slices"),
            ("lines", "SELECT COALESCE(SUM(lines), 0) FROM materials"),
        ):
            try:
                scalars[name] = int(cur.execute(sql).fetchone()[0])
            except sqlite3.OperationalError:
                scalars[name] = 0
        info["scalars"] = scalars
        return info
    finally:
        conn.close()


def print_size(db: Path) -> int:
    info = describe(db)
    print("=== 文件 ===")
    print("   %-34s %8.1f MB" % (info["file"], info["bytes"] / 1048576))
    print("   page_size=%s page_count=%s freelist=%s（空页 %d 页）" % (
        info["pageSize"], info["pageCount"], info["freelist"],
        info["freelist"]))
    print("=== 各表实际占用（%s）===" % info["tableBytesSource"])
    for row in info["tables"][:14]:
        print("   %-46s %9.1f MB  %6.1f%%" % (row["name"], row["bytes"] / 1048576, row["share"] * 100))
    vectors = info.get("vectors")
    if vectors:
        print("=== 向量那一列 ===")
        print("   %s：%d 条 · 平均 %.0f 字节 · 最大 %d" % (
            vectors["storedAs"], vectors["count"], vectors["avgBytes"], vectors["maxBytes"]))
        for one in info["models"]:
            print("   %s · dim=%d · %d 条" % (one["model"], one["dim"], one["count"]))
        print("   净重 %.1f MB。参照：float32 = dim×4 = 4096 字节；float16 = 2048；int8 = 1024" % (
            vectors["netBytes"] / 1048576))
    scalars = info["scalars"]
    print("=== 规模 ===")
    print("   材料 %d 本 · 切片 %d 片 · 窗口 %s 个 · 正文 %d 行" % (
        scalars["materials"], scalars["slices"],
        vectors["count"] if vectors else "?", scalars["lines"]))
    if vectors and scalars["lines"]:
        per_line = vectors["netBytes"] / scalars["lines"]
        print("   每 %d 行正文一个窗口 · 每 1000 行约 %.2f MB" % (
            round(scalars["lines"] / vectors["count"]), per_line * 1000 / 1048576))
        print("=== 推算（按现在这份编码）===")
        base = info["bytes"] / 1048576 - vectors["netBytes"] / 1048576
        for lines, label in LINE_SAMPLES:
            add = per_line * lines / 1048576
            print("   %-22s → 向量 +%7.0f MB（整库 %.1f GB）" % (label, add, (base + add) / 1024))
    return 0


def print_counts(db: Path) -> int:
    conn = connect(db)
    try:
        cur = conn.cursor()
        names = [row[0] for row in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        print("=== 行数 ===")
        for name in names:
            try:
                count = cur.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            except sqlite3.OperationalError:
                continue
            print("   %-30s %9d" % (name, count))
        for title, sql in (
            ("材料按档位", "SELECT depth, COUNT(*) FROM materials GROUP BY depth ORDER BY 2 DESC"),
            ("材料按学科", "SELECT subject, COUNT(*) FROM materials GROUP BY subject ORDER BY 2 DESC"),
        ):
            try:
                rows = cur.execute(sql).fetchall()
            except sqlite3.OperationalError:
                continue
            if rows:
                print("=== %s ===" % title)
                for key, count in rows:
                    print("   %-30s %9d" % (key, count))
        return 0
    finally:
        conn.close()


def run_query(db: Path, sql: str, limit: int) -> int:
    conn = connect(db)
    try:
        cur = conn.cursor()
        try:
            cur.execute(sql)
        except sqlite3.OperationalError as exc:
            print(f"这句没跑通：{exc}", file=sys.stderr)
            return 1
        titles = [one[0] for one in (cur.description or [])]
        rows = cur.fetchmany(max(1, limit))
        more = 1 if cur.fetchone() else 0
        if titles:
            widths = [len(str(one)) for one in titles]
            for row in rows:
                for index, value in enumerate(row[:len(widths)]):
                    widths[index] = max(widths[index], len("" if value is None else str(value)))
            line = "  ".join(str(name).ljust(widths[index]) for index, name in enumerate(titles))
            print(line)
            print("-" * len(line))
            for row in rows:
                print("  ".join(
                    ("" if value is None else str(value)).ljust(widths[index])
                    for index, value in enumerate(row[:len(widths)])
                ))
        print("（%d 行%s）" % (len(rows), "，还有更多（加 --limit）" if more else ""))
        return 0
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tools/db_ops.py",
        description="数据库运维：体积 / 行数 / 随手查（**只读**，不改库）",
    )
    parser.add_argument("--db", default=str(DEFAULT_DB), help="库路径（默认 data/quizforge.db）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    size_parser = sub.add_parser("size", help="体积构成：哪个表占地方、向量怎么存的")
    size_parser.add_argument("--json", action="store_true", help="输出原始数（给程序）")

    counts_parser = sub.add_parser("counts", help="各行数与分布")
    counts_parser.add_argument("--json", action="store_true")

    query_parser = sub.add_parser("query", help="只读查询一句 SQL")
    query_parser.add_argument("sql")
    query_parser.add_argument("--limit", type=int, default=50)

    args = parser.parse_args(argv)
    db = Path(args.db)
    if args.cmd == "size":
        if args.json:
            print(json.dumps(describe(db), ensure_ascii=False, indent=1))
            return 0
        return print_size(db)
    if args.cmd == "counts":
        return print_counts(db)
    return run_query(db, args.sql, args.limit)


if __name__ == "__main__":
    raise SystemExit(main())
