"""发布：把库里**已校验**的题翻转成**已发布**。

原来这一层叫"统合 / 提升"，干的是把 `staged/*.md` 搬进 `questions/` 并改名。
题库进了数据库之后，它只剩三件事：

  ① **topic 归一**：出题员会把候选点的 key 直接当 topic 写（例如
     `tensix-internal-components`），或挂在非叶子节点上。这里按一张显式映射表
     归到考纲里真实存在的 key；表里没有的报出来、**不改写、不猜**。
  ② **发布前格式闸门**：把库物化到临时目录，跑 `tools/check.py`（与构建、自测同一套规则）。
     不合格的题**留在"已校验"**，不进正式题库，也就不会出现在任何人的复习界面里。
  ③ **状态翻转**：`verified` → `published`。

用法：
    api/.venv/bin/python -m pipeline.promote            # 预演
    api/.venv/bin/python -m pipeline.promote --apply    # 落库
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from sqlalchemy import text

from . import bankfile, config

sys.path.insert(0, str(config.ROOT / "api"))

# 候选点的 key → 考纲 key。
# 只有「名字对不上」的才需要映射；能直接对上的原样通过。
# 判据：写成考纲里**最贴近的叶子**，实在没有合适叶子就挂到单元上（考纲允许任意一层，
# 按上层筛选会含子孙），但绝不为了好看去造一个没人用的 key。
TOPIC_MAP = {
    # 候选点 key 直接当 topic 用（出题员常见失误）
    "tensix-internal-components": "tt-tensix",
    "baby-riscv-role": "tt-tensix-riscv",
    "tensix-processor-grid-of-nodes": "tt-tensix",
    # 单元级 / 旧名 → 现在的叶子
    "tt-metal-noc": "tt-noc",
    "tt-metal-no-cache": "tt-metal-memory-placement",
    "tt-metal-memory": "tt-metal-memory-placement",
    "tt-metalium": "tt-metalium-api",
    "tt-metal-cb": "tt-metal-circular-buffer",
    "tt-metal-dataflow-cb": "tt-metal-circular-buffer",
    # 点是「材料里的一个可考事实簇」，topic 是「考纲里的考点」——两套命名空间，
    # 出题员会拿点的 key 当 topic。能对上既有叶子的就在这里对上，对不上才补考纲，
    # 两者都不做的话，答案就是「把题硬塞进一个不合适的 key」。
    "tt-metal-fusion": "tt-metal-kernel",
    "tensix-noc-near-memory": "tt-noc-topology",
    "tt-tensix-datapath": "tt-tensix-fpu",
}

# 从 check.py 的输出里认出题号（它打的是文件路径，文件名里带 id）
ID_RE = re.compile(r"\b([a-z][a-z0-9]*(?:-[a-z0-9]+)*-\d{4})\b")


def _engine():
    from app.db import get_engine  # noqa: PLC0415

    return get_engine()


def topic_keys() -> set[str]:
    """考纲 key 集合（权威在库：`topics` 表）。"""
    with _engine().connect() as conn:
        return {row[0] for row in conn.execute(text("SELECT key FROM topics"))}


def pending() -> list[dict]:
    """待发布的题：状态为 `verified`。"""
    with _engine().connect() as conn:
        rows = conn.execute(
            text("SELECT id, topic, payload FROM questions WHERE status = 'verified' ORDER BY id")
        ).fetchall()
    return [dict(row._mapping) for row in rows]


def failed_by_gate() -> set[str]:
    """发布前格式闸门：物化到临时目录跑 `tools/check.py`，返回**不合格**的题号集合。"""
    with tempfile.TemporaryDirectory(prefix="qf-gate-") as tmp:
        root = Path(tmp)
        bankfile.materialize(root, statuses=("verified", "published"))
        env = {
            **os.environ,
            "QF_QUESTIONS_DIR": str(root / "questions"),
            "QF_TOPICS_FILE": str(root / "meta" / "topics.yaml"),
        }
        proc = subprocess.run(
            [sys.executable, str(config.ROOT / "tools" / "check.py")],
            capture_output=True,
            text=True,
            env=env,
        )
    bad: set[str] = set()
    for line in (proc.stdout or "").splitlines():
        if "ERROR" not in line:
            continue
        match = ID_RE.search(line)
        if match:
            bad.add(match.group(1))
    return bad


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.promote", description="发布已校验的题")
    parser.add_argument("--apply", action="store_true", help="落库（默认只预演）")
    parser.add_argument("--limit", type=int, default=0, help="最多发布多少道（0=不限）")
    args = parser.parse_args(argv)

    rows = pending()
    if not rows:
        print("没有待发布的题（状态 `verified`）。")
        return 0
    if args.limit:
        rows = rows[: args.limit]

    known = topic_keys()

    # 先把 topic 归一**落库**，再跑格式闸门。
    # 顺序不能反：闸门要检查的是"发布出去的样子"，而出题员常把**点 key** 直接当 topic 写
    # （实测一批 13 道题就是这样，闸门看的是原始 topic，于是全被拦下 —— 拦得对，
    # 但拦错了对象：该归一的归一之后它们本来就是合法的）。
    remapped: list[str] = []
    with _engine().begin() as conn:
        for row in rows:
            topic = str(row["topic"] or "")
            mapped = TOPIC_MAP.get(topic, topic)
            if mapped not in known:
                mapped = str(row["id"]).rsplit("-", 1)[0] or "tt-arch"
            if mapped == topic:
                continue
            payload = dict(row["payload"] or {})
            payload["topic"] = mapped
            conn.execute(
                text(
                    "UPDATE questions SET topic = :topic, payload = CAST(:payload AS JSONB),"
                    " updated_at = now() WHERE id = :id"
                ),
                {
                    "topic": mapped,
                    "payload": json.dumps(payload, ensure_ascii=False),
                    "id": row["id"],
                },
            )
            remapped.append(f"{row['id']}  {topic} → {mapped}")
            row["topic"] = mapped
    if remapped:
        print(f"topic 归一 {len(remapped)} 道：")
        for line in remapped[:8]:
            print(f"  {line}")
        if len(remapped) > 8:
            print(f"  …（另有 {len(remapped) - 8} 道）")

    bad = failed_by_gate()

    plan: list[dict] = []
    for row in rows:
        if row["id"] in bad:
            plan.append({**row, "skip": "格式未过闸门"})
            continue
        topic = str(row["topic"] or "")
        mapped = TOPIC_MAP.get(topic, topic)
        note = ""
        if mapped not in known:
            mapped = str(row["id"]).rsplit("-", 1)[0] or "tt-arch"
            note = f"考纲没有 {topic}，暂挂学科级 {mapped}"
        plan.append({**row, "topic_new": mapped, "note": note, "skip": ""})

    ok = [item for item in plan if not item["skip"]]
    print(f"待发布 {len(rows)} 道 → 可发布 {len(ok)} 道（格式未过 {len(plan) - len(ok)}）")
    for item in plan:
        if item["skip"]:
            print(f"  跳过 {item['id']}：{item['skip']}")
        elif item["note"]:
            print(f"  {item['id']}  {item['topic']} → {item['topic_new']}（{item['note']}）")

    if not args.apply:
        print("\n（预演。加 --apply 落库。）")
        return 0

    with _engine().begin() as conn:
        for item in ok:
            payload = dict(item["payload"] or {})
            payload["topic"] = item["topic_new"]
            conn.execute(
                text(
                    "UPDATE questions SET topic = :topic, payload = CAST(:payload AS JSONB),"
                    " status = 'published', updated_at = now() WHERE id = :id"
                ),
                {
                    "topic": item["topic_new"],
                    "payload": json.dumps(payload, ensure_ascii=False),
                    "id": item["id"],
                },
            )
    print(f"\n已发布 {len(ok)} 道。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
