#!/usr/bin/env python3
"""数据快照的存取 —— **快照就是 SQLite 库本身**，不是某种导出版本。

## 为什么长这样

题库、考纲、知识空间的唯一权威在库（`data/quizforge.db`），而库里那些是几个月
攒下来的东西，不能重来一遍。所以"数据进版本库"必须有个载体。

原先那个载体是 `db/quizforge.sql.gz` —— 一份 `pg_dump` 的产物。它的代价是：
**要把它变成能用的库，得先起一台 Postgres**（`docker compose up` → `psql` 灌进去
→ 再用 `tools/migrate_to_local.py` 搬进 SQLite）。于是"换台机器接着干"这件事
被一整套 Docker + Postgres 绑住了，而这个应用本身**根本不连数据库服务**。

2026-09-22 换掉：快照直接用 SQLite 库本身（gzip 压缩）。一条命令解压即用，
仓库里不再有任何需要 Docker 或 Postgres 的路径。`migrate_to_local.py` 保留着 ——
它是**一次性搬运**的工具（历史数据还在 Postgres 里的那种场合），日常用不到。

## 两条命令

    python3 tools/db_snapshot.py save --out db/quizforge.db.gz   # 库 → 快照
    python3 tools/db_snapshot.py load --path db/quizforge.db.gz  # 快照 → 库（--force 覆盖）
    python3 tools/db_snapshot.py load --path … --target /tmp/x.db  # 换个落点（自检用）

## 两件不许省的事

* **落盘前抹掉 AI 密钥**。`user_settings.data.ai.apiKey` 存着用户自己填的密钥，
  而快照是进版本控制的（仓库公开）。2026-09-17 撞过一次真事故：一把真的
  DeepSeek 密钥跟着快照上了公开仓库，追下去有十一个提交；事后清理没有意义
  （旧对象仍可按 SHA 取到），只能换密钥。这里抹的是**快照里的那份**，库里照旧。
  出口还有一道 `tools/secret_scan.py` 兜底（`make check` 里跑）。

* **载入时校验完整性**。一个坏了或半截的快照，宁可当场拒绝，也不要给出一份
  "看着能跑、其实少了一半题"的库 —— 那种问题会在几天后以"某道题怎么不见了"
  的形式冒出来，届时已经查不清是哪一步坏的。校验两项：SQLite 的 `integrity_check`
  与 `foreign_key_check`（悬空引用是搬运/截断最典型的翻车方式）。
"""

from __future__ import annotations

import argparse
import gzip
import json
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "quizforge.db"

#: 快照里必须被抹空的字段路径（设置表 `data` 里那棵树）。
_SECRET_PATH = ("ai", "apiKey")

#: 设置表的名字 —— 2026-09-22 从 `user_settings` 改名而来（账号系统整体拆除）。
#: 留一个常量，改名时只动这一处。
_SETTINGS_TABLE = "app_settings"


def _strip_secrets(conn: sqlite3.Connection) -> int:
    """把快照里 `app_settings.data.ai.apiKey` 抹空，返回改了几行（0 或 1）。

    只在 `data` 能被解析成 JSON 对象、且里面真有 `ai` 对象时才动它 ——
    一个字段都不多改，免得把设置里的别的东西搅坏。

    表不存在时返回 0（不炸）：`app_settings` 是 2026-09-22 才改的名
    （原 `user_settings`），而快照工具可能被指向一份更老的库。
    那种库抹不了密钥，但也不该让整条命令失败 —— 出口的 `secret_scan` 会拦。
    """
    try:
        row = conn.execute(f"SELECT id, data FROM {_SETTINGS_TABLE}").fetchone()
    except sqlite3.OperationalError:
        return 0
    if row is None:
        return 0

    row_id, raw = row
    try:
        data = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    except (ValueError, TypeError):
        return 0
    if not isinstance(data, dict):
        return 0
    node = data.get(_SECRET_PATH[0])
    if not isinstance(node, dict) or _SECRET_PATH[1] not in node:
        return 0
    if not node[_SECRET_PATH[1]]:
        return 0  # 本来就是空的，不必写回去
    node[_SECRET_PATH[1]] = ""
    conn.execute(
        f"UPDATE {_SETTINGS_TABLE} SET data = ? WHERE id = ?",
        (json.dumps(data, ensure_ascii=False), row_id),
    )
    return 1


def _verify(path: Path) -> list[str]:
    """对一个库文件做完整性体检，返回问题清单（空 = 干净）。"""
    problems: list[str] = []
    conn = sqlite3.connect(str(path))
    try:
        check = conn.execute("PRAGMA integrity_check").fetchone()
        if not check or str(check[0]).lower() != "ok":
            problems.append(f"integrity_check 不通过：{check[0] if check else '无返回'}")
        dangling = conn.execute("PRAGMA foreign_key_check").fetchall()
        if dangling:
            problems.append(f"有 {len(dangling)} 处悬空引用（第一处：{dangling[0]}）")
    finally:
        conn.close()
    return problems


def _summary(path: Path) -> str:
    """快照的一句话摘要（题数等），给人看 —— 存完读一下就知道对不对。"""
    try:
        conn = sqlite3.connect(str(path))
    except sqlite3.Error:
        return ""
    try:
        count = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
        return (
            f"现行题 {count('SELECT COUNT(*) FROM questions WHERE retired_at IS NULL')} · "
            f"主题 {count('SELECT COUNT(*) FROM topics')} · "
            f"概念 {count('SELECT COUNT(*) FROM concepts')}"
        )
    except sqlite3.Error:
        return ""
    finally:
        conn.close()


def save(out: Path, db: Path = DEFAULT_DB) -> int:
    if not db.is_file():
        print(f"没有库可存：{db} 不存在", file=sys.stderr)
        return 1

    tmp = out.with_suffix(out.suffix + ".tmp")
    out.parent.mkdir(parents=True, exist_ok=True)

    # 用 SQLite 自己的 backup API 取快照，而不是直接拷文件：
    # 库开着 WAL，直接拷会把 -wal 里还没并回去的事务漏掉（读起来就是"少了一批题"）。
    # backup 是事务一致的，且不要求先停服务。
    stage = tmp.with_suffix(".stage")
    stage.unlink(missing_ok=True)
    source = sqlite3.connect(str(db))
    staged = sqlite3.connect(str(stage))
    try:
        source.backup(staged)
    finally:
        staged.close()
        source.close()

    # 抹密钥 → 压实。顺序要紧：**先抹再 VACUUM**，
    # 否则被抹掉的明文可能还留在页的空隙里（VACUUM 之后才真的写不进快照）。
    conn = sqlite3.connect(str(stage))
    try:
        stripped = _strip_secrets(conn)
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()

    with open(stage, "rb") as fi, gzip.open(tmp, "wb", compresslevel=9) as fo:
        shutil.copyfileobj(fi, fo, 1 << 20)
    stage.unlink(missing_ok=True)
    tmp.replace(out)

    size = out.stat().st_size / 1024 / 1024
    print(f"快照已写入 {out}（{size:.2f} MB）")
    print(f"  抹掉密钥 {stripped} 处 · {_summary(db)}")
    return 0


def load(path: Path, target: Path, force: bool = False) -> int:
    if not path.is_file():
        print(f"快照不存在：{path}", file=sys.stderr)
        return 1
    if target.exists() and not force:
        print(
            f"库里已经有东西了：{target}\n"
            f"  要覆盖请加 --force（会先删掉它）。",
            file=sys.stderr,
        )
        return 2

    # 先解到一个临时文件并**校验**，通过了再替换正式库：
    # 校验失败的快照不该先把好库毁掉 —— "解压一半失败"要留下可回退的现场。
    #
    # `mkdir` 必须在这里（**写之前**）：新机器上 `data/` 根本不存在，
    # 而这一步是首次落盘的地方。原先它排在下面替换正式库之前 ——
    # 结果"解压"先撞上"目录不存在"，干净克隆的第二条命令就断在这儿
    #（实测：新 agent 照着 README 走，`make api-venv` 成功、`make db-restore` 直接崩）。
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_suffix(target.suffix + ".incoming")
    staging.unlink(missing_ok=True)
    with gzip.open(path, "rb") as fi, open(staging, "wb") as fo:
        shutil.copyfileobj(fi, fo, 1 << 20)

    problems = _verify(staging)
    if problems:
        staging.unlink(missing_ok=True)
        print(f"快照没通过校验，已拒绝载入：{path}", file=sys.stderr)
        for item in problems:
            print(f"  ✗ {item}", file=sys.stderr)
        return 1

    for sidecar in (target.with_name(target.name + "-wal"), target.with_name(target.name + "-shm")):
        sidecar.unlink(missing_ok=True)  # 旧库的 WAL 不能留着和新库混在一起
    staging.replace(target)

    print(f"已载入 {path} → {target}")
    print(f"  {_summary(target)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tools/db_snapshot.py",
        description="数据快照的存取（快照 = SQLite 库本身，gzip 压缩）",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    save_parser = sub.add_parser("save", help="库 → 快照（抹密钥后落盘）")
    save_parser.add_argument("--out", default="db/quizforge.db.gz")
    save_parser.add_argument("--db", default=str(DEFAULT_DB), help="源库路径（默认 data/quizforge.db）")

    load_parser = sub.add_parser("load", help="快照 → 库（载入前校验）")
    load_parser.add_argument("--path", default="db/quizforge.db.gz")
    load_parser.add_argument("--target", default=str(DEFAULT_DB), help="落点（默认 data/quizforge.db）")
    load_parser.add_argument("--force", action="store_true", help="目标已存在也覆盖")

    args = parser.parse_args(argv)
    if args.cmd == "save":
        return save(Path(args.out), Path(args.db))
    return load(Path(args.path), Path(args.target), force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())
