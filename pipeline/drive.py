"""状态机自动跑：**出题 → 校验 → 发布 → 打回重出**，一直转到收敛。

状态机的三样东西本来就有：任务表是状态 ✔、worker 是执行 ✔、rework 是回流 ✔。
缺的只是一个把它连着转起来、并在收敛时停下的驱动 —— 手动跑四步迟早会漏掉第三步
（实测：失败题就是这么烂在草稿里的 ✗）。

**收敛判据**：一轮下来既没有新发布、也没有新退役、队列也没有待办 → 停。
**防跑飞**：轮数上限 + 每轮 worker 的任务上限 + 单轮预算（worker 自带闸门）。

用法：

    api/.venv/bin/python -m pipeline.drive                    # 默认最多 6 轮
    api/.venv/bin/python -m pipeline.drive --material X --iterations 2
    api/.venv/bin/python -m pipeline.drive --dry-run          # 只打印将要做什么
"""

from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
import time

from . import config

sys.path.insert(0, str(config.ROOT / "api"))

PY = str(config.ROOT / "api" / ".venv" / "bin" / "python")


def _run(args: list[str], quiet: bool = True) -> tuple[int, list[str]]:
    proc = subprocess.run(
        [PY, "-m", *args], cwd=str(config.ROOT), capture_output=True, text=True
    )
    lines = [line for line in (proc.stdout or "").strip().splitlines() if line.strip()]
    if proc.returncode != 0 and quiet:
        lines += [line for line in (proc.stderr or "").strip().splitlines()[-3:]]
    return proc.returncode, lines


def _todo() -> dict[str, int]:
    conn = sqlite3.connect(str(config.ROOT / "pipeline" / "state.sqlite3"))
    try:
        return {
            kind: count
            for kind, count in conn.execute(
                "SELECT kind, COUNT(*) FROM tasks WHERE status IN ('todo', 'running') GROUP BY kind"
            )
        }
    finally:
        conn.close()


def _counts() -> dict[str, int]:
    from sqlalchemy import text

    from app.db import get_engine

    with get_engine().connect() as conn:
        row = conn.execute(
            text(
                "SELECT COUNT(*) FILTER (WHERE status = 'published'),"
                " COUNT(*) FILTER (WHERE status = 'verified'),"
                " COUNT(*) FILTER (WHERE status = 'draft' AND retired_at IS NULL),"
                " COUNT(*) FILTER (WHERE retired_at IS NOT NULL) FROM questions"
            )
        ).fetchone()
    return {"published": row[0], "verified": row[1], "draft": row[2], "retired": row[3]}


def materials_with_gaps(limit: int) -> list[str]:
    """缺口最大的几份材料 —— 一轮里只推它们，避免一次铺太开。"""
    from . import coverage

    rows = coverage.rows_for(None)
    gaps = [r for r in rows if r["covered"] < r["points"]]
    gaps.sort(key=lambda r: r["covered"] / max(1, r["points"]))
    return [r["slug"] for r in gaps[:limit]]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.drive", description="状态机自动跑")
    parser.add_argument("--iterations", type=int, default=6, help="最多几轮（每轮会花 token）")
    parser.add_argument("--material", help="只推某一份材料（默认：缺口最大的 3 份）")
    parser.add_argument("--per-round-materials", type=int, default=3)
    parser.add_argument("--max-tasks", type=int, default=12, help="每轮 worker 处理多少任务")
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true", help="只打印将要做什么")
    args = parser.parse_args(argv)

    for i in range(1, args.iterations + 1):
        before = _counts()
        todo = _todo()
        print(f"=== 第 {i} 轮 ===（队列 {todo or '空'} · 已发布 {before['published']}）")
        if args.dry_run:
            targets = [args.material] if args.material else materials_with_gaps(args.per_round_materials)
            print(f"  将推：{targets}")
            print("  dispatch → worker → promote → rework")
            continue

        slugs = [args.material] if args.material else materials_with_gaps(args.per_round_materials)
        for slug in slugs:
            code, out = _run(["pipeline.dispatch", "--material", slug, "--kind", "all"])
            print(f"  派工 {slug[:40]:40s} {out[-1] if out else code}")

        code, out = _run(
            [
                "pipeline.worker",
                "--kind",
                "all",
                "--concurrency",
                str(args.concurrency),
                "--max",
                str(args.max_tasks),
            ]
        )
        tail = [line for line in out if "结束：" in line]
        print(f"  执行 {tail[-1] if tail else (out[-1] if out else code)}")

        code, out = _run(["pipeline.promote", "--apply"])
        print(f"  发布 {out[-1] if out else code}")

        code, out = _run(["pipeline.rework", "--apply"])
        print(f"  回流 {out[-1] if out else code}")

        after = _counts()
        todo_after = _todo()
        progressed = (
            after["published"] > before["published"] or after["retired"] > before["retired"]
        )
        print(
            f"  → 已发布 {after['published']}（+{after['published'] - before['published']}）"
            f" · 退役 {after['retired']}（+{after['retired'] - before['retired']}）"
            f" · 队列 {todo_after or '空'}"
        )
        if not todo_after and not progressed:
            print("\n收敛：队列空、也没有新产出 —— 停。")
            break
        time.sleep(1)

    final = _counts()
    print(
        f"\n收尾：已发布 {final['published']} · 已校验待发布 {final['verified']}"
        f" · 草稿 {final['draft']} · 已退役 {final['retired']} · 队列 {_todo() or '空'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
