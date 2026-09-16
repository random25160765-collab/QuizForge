"""看板：任务表的状态、账本与失败清单。

用法：
    api/.venv/bin/python -m pipeline.status
"""

from __future__ import annotations

import json

from . import config, store


def main(argv: list[str] | None = None) -> int:
    print(f"库：{config.DB_FILE}")
    conn = store.connect()

    summary = store.summary(conn)
    print("\n按 kind / status：")
    print(json.dumps(summary["by_kind"], ensure_ascii=False, indent=2))
    print(
        f"\n账本合计：输入 {summary['tokens_in']:,} tokens · 输出 {summary['tokens_out']:,} tokens"
    )

    rows = conn.execute(
        "SELECT status, COUNT(*) AS n, "
        "ROUND(SUM(COALESCE(finished_at, started_at, created_at) - started_at), 1) AS secs "
        "FROM tasks GROUP BY status"
    ).fetchall()
    print("\n耗时：")
    for row in rows:
        print(f"  {row['status']:<8} {row['n']:>4} 个")

    fails = store.failures(conn, limit=8)
    if fails:
        print("\n失败清单（最近 8 条）：")
        for row in fails:
            print(f"  [{row['kind']}] {row['id']}  attempts={row['attempts']}  {row['error']}")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
