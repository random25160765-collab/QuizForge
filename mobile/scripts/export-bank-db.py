#!/usr/bin/env python3
"""从本机权威库里抽出一个「只有题库」的 SQLite 文件，供 APK 内置。

为什么不直接拷 `data/quizforge.db`：

* 它 250M，其中 168M 是 `slice_embeddings`（derived，能在手机上重算吗？不能 ——
  但手机端也跑不了向量化，所以索引本来就用不上）；更大的问题是它**含用户数据**
  （对话、进度、设置里的密钥），打包进 APK 等于把隐私发出去。
* 题库表（`datapack.BOUNDARY` 里 kind=bank 的那 16 张）是「出题机产物」，与用户
  无关，正是要随包分发的部分。

所以这里按 `BANK_TABLES` 建一个干净的新库：先照抄原表的 CREATE 语句与索引，
再按表拷贝数据。用户表与 `qf_sync_*` 一张都不进来。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "api"))

from app.datapack import BANK_TABLES  # noqa: E402  —— 需要先设好 sys.path


def build(source: Path, target: Path) -> None:
    if not source.is_file():
        raise SystemExit(f"源库不存在：{source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)

    con = sqlite3.connect(target)
    try:
        con.execute("ATTACH DATABASE ? AS src", (str(source),))
        copied = 0
        for name in BANK_TABLES:
            row = con.execute(
                "SELECT sql FROM src.sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone()
            if not row or not row[0]:
                print(f"  跳过（源库里没有）：{name}")
                continue
            con.execute(row[0])
            con.execute(f'INSERT INTO "{name}" SELECT * FROM src."{name}"')
            for (index_sql,) in con.execute(
                "SELECT sql FROM src.sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
                (name,),
            ):
                con.execute(index_sql)
            count = con.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
            print(f"  {name:24s} {count:>7} 行")
            copied += 1
        con.commit()
        con.execute("DETACH DATABASE src")
        con.execute("VACUUM")
        print(f"完成：{copied} 张表 → {target} ({target.stat().st_size / 1048576:.1f} MB)")
    finally:
        con.close()


def main() -> None:
    source = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "quizforge.db"
    target = (
        Path(sys.argv[2])
        if len(sys.argv) > 2
        else ROOT / "mobile" / "android" / "app" / "src" / "main" / "assets" / "bank.db"
    )
    build(source, target)


if __name__ == "__main__":
    main()
