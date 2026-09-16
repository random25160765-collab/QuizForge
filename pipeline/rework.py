"""打回重出：**失败的题不再烂在草稿里**。

每道被校验打回的题，最终必须落到两种归宿之一 —— 这就是"不要漏了"的保证：

* **重出**：这个点还没被别的题覆盖 → 为它单发一个出题任务（带缺陷清单与轮次）；
* **退役**：这个点已经被通过的题覆盖了，或已经重出过 `MAX_ROUND` 轮 → 退役
  （**退役而不是删除**：题目 id 是稳定资产，删了就没法解释"这道题去哪了"）。

为什么不把这段逻辑放回 worker 的串联里：实测"校验失败 → 立刻串联重出"是**正反馈**
（队列从 1200 涨到 3700：重出又生成新校验，环环相扣）。放在这里就变成一次
**显式的批量动作**：有 dry-run、有轮次上限、每道题只会被处理一次，
而且跑完能明确报出"重出 N 道 / 退役 M 道 / 可疑的点 K 个"。

用法：

    api/.venv/bin/python -m pipeline.rework              # 预演
    api/.venv/bin/python -m pipeline.rework --apply      # 真派工 + 真退役
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from sqlalchemy import text

from . import config, store
from .worker import prompt_version

sys.path.insert(0, str(config.ROOT / "api"))

MAX_ROUND = 2

PLAN_SQL = text(
    """
    SELECT q.id, q.topic, q.payload, q.verify_report,
           COALESCE((q.payload ->> 'round')::int, 0) AS round,
           kp.id AS point_id, kp.key AS point_key, kp.name AS point_name, kp.kind AS point_kind,
           kp.thickness, kp.layers, kp.note,
           m.slug AS material, m.source_path,
           EXISTS (
             SELECT 1 FROM question_points qp2
             JOIN questions q2 ON q2.id = qp2.question_id
             WHERE qp2.point_id = kp.id AND q2.status IN ('published', 'verified')
           ) AS covered
    FROM questions q
    LEFT JOIN knowledge_points kp
           ON kp.key = COALESCE(NULLIF(q.payload ->> 'point', ''), q.topic)
    LEFT JOIN materials m ON m.id = kp.material_id
    WHERE q.status = 'draft' AND q.verify_report IS NOT NULL
      AND q.retired_at IS NULL  -- 已退役的不再挑出来（否则每轮都要重新处理一遍）
    ORDER BY q.id
    """
)


def plan(limit: int = 0) -> list[dict]:
    from . import dbstore  # noqa: PLC0415

    with dbstore._engine().connect() as conn:  # noqa: SLF001
        rows = [dict(row._mapping) for row in conn.execute(PLAN_SQL)]
    out: list[dict] = []
    for row in rows:
        if row["covered"]:
            out.append({**row, "action": "retire", "why": "该点已有通过的题"})
        elif not row["point_id"]:
            out.append({**row, "action": "retire", "why": "认不出它考的是哪个点（无法重出）"})
        elif int(row["round"] or 0) >= MAX_ROUND:
            out.append({**row, "action": "retire", "why": f"已重出 {row['round']} 轮仍不合格"})
        else:
            out.append({**row, "action": "retry", "why": f"第 {int(row['round'] or 0) + 1} 轮重出"})
    return out[:limit] if limit else out


def build_task(row: dict) -> dict:
    payload = row.get("payload") or {}
    return {
        "map": row["material"] or "",
        "material": Path(row["source_path"] or "").name,
        "subject": "tt-arch",
        "source_path": row["source_path"] or "",
        "pack_id": f"rework-{row['id']}",
        # 只出**这一个点**：整包重出会让一次失败放大成一包的浪费
        "points": [
            {
                "key": row["point_key"],
                "name": row["point_name"],
                "kind": row["point_kind"],
                "thickness": row["thickness"],
                "layers": row["layers"] or [],
                "terms": [],
                "note": row["note"] or "",
            }
        ],
        "layer_target": row["layers"] or [],
        "start_index": None,
        "existing": [],
        "out_dir": "",
        "prompt_version": prompt_version("author"),
        "ranges": payload.get("verify_ranges") or payload.get("sources") or [],
        # 轮次 + 缺陷清单：重出的题必须带着上一次错在哪 —— 否则就是掷骰子
        "round": int(row["round"] or 0) + 1,
        "feedback": (row.get("verify_report") or {}).get("problems") or [],
        "rewrite": [row["id"]],
    }


def run(rows: list[dict], apply: bool) -> dict:
    from . import dbstore  # noqa: PLC0415

    conn = store.connect()
    stats = {"retry": 0, "retire": 0, "skipped": 0}
    retried_points: set[str] = set()
    try:
        for row in rows:
            if row["action"] == "retry" and row["point_key"] in retried_points:
                stats["skipped"] += 1  # 一个点只重出一份，别让同一个缺口排两次队
                continue
            if row["action"] == "retry":
                task = build_task(row)
                digest = hashlib.sha256(
                    (row["id"] + json.dumps(task["feedback"], ensure_ascii=False)).encode("utf-8")
                ).hexdigest()[:16]
                created = store.add_task(conn, "author", digest, task)
                stats["retry" if created else "skipped"] += 1
                retried_points.add(row["point_key"])
                if not apply:
                    continue
            stats["retire"] += (row["action"] == "retire")
            if apply:
                with dbstore._engine().begin() as db:  # noqa: SLF001
                    db.execute(
                        text("UPDATE questions SET retired_at = now(), updated_at = now() WHERE id = :id"),
                        {"id": row["id"]},
                    )
    finally:
        conn.close()
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.rework", description="打回重出 / 退役")
    parser.add_argument("--apply", action="store_true", help="真的派工与退役；不加只预演")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args(argv)

    rows = plan(args.limit)
    if not rows:
        print("没有烂在草稿里的题 ✔（每道失败题都已落到重出或退役）")
        return 0

    retry = [r for r in rows if r["action"] == "retry"]
    retire = [r for r in rows if r["action"] == "retire"]
    print(f"失败草稿 {len(rows)} 道 → 重出 {len(retry)} 道 · 退役 {len(retire)} 道{'（预演）' if not args.apply else ''}")
    for row in retry:
        print(f"  ↻ {row['id']:14s} {row['why']:12s} 点 {row['point_key']}")
    for row in retire[:10]:
        print(f"  ⏹ {row['id']:14s} {row['why']}")
    if len(retire) > 10:
        print(f"  ⏹ …（另有 {len(retire) - 10} 道）")

    stats = run(rows, args.apply)
    print(
        f"\n派工 {stats['retry']} 道重出 · 退役 {stats['retire']} 道 · 跳过重复 {stats['skipped']} 道"
        + ("（已落库）" if args.apply else "（预演，未落库）")
    )
    if args.apply and stats["retry"]:
        print("下一步：跑 worker（--kind all），重出的题会照常走校验 → 发布")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
