"""修复员：把"只差一点"的题**救回来**，而不是整道重出。

为什么值得单独一步：打回之后重出，等于把这道题里已经做对的部分（好的干扰项、
清楚的题面、贴切的依据）全丢掉。实测这份材料里，缺陷有三分之一是机械的
（层级标错、小节名不合规、缺小节）—— 那些不该整道重出。

三条纪律（写在 `prompts/fix.md` 里，这里只负责执行）：

1. **最小改动** —— 只改缺陷指向的字段；
2. **不许换题** —— 基本设定不成立就如实放弃（`repairable: false`）；
3. **改完必须由原校验员复检** —— 修复员自己说"好了"不算，那等于自己批自己。

用法：

    api/.venv/bin/python -m pipeline.fix --limit 8            # 预演：只列出要处理哪些
    api/.venv/bin/python -m pipeline.fix --limit 8 --apply    # 真改（改完再去跑校验）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from sqlalchemy import text

from . import config, dbstore
from .llm import LLM, parse_json
from .worker import load_prompt, read_ranges, render

sys.path.insert(0, str(config.ROOT / "api"))

# 机械缺陷（改一处即可）与内容缺陷（可能要放弃）—— 只用于挑选与统计，不用于限制修复员
MECHANICAL = {"layer", "format", "distractor", "tag", "unique"}

DRAFTS_SQL = text(
    """
    SELECT q.id, q.type, q.topic, q.raw_markdown, q.payload, q.verify_report,
           COALESCE(m.source_path, '') AS source_path
    FROM questions q
    LEFT JOIN knowledge_points kp
           ON kp.key = COALESCE(NULLIF(q.payload ->> 'point', ''), q.topic)
    LEFT JOIN materials m ON m.id = kp.material_id
    WHERE q.status = 'draft' AND q.verify_report IS NOT NULL
    ORDER BY q.id
    """
)


def drafts(material: str | None, limit: int) -> list[dict]:
    with dbstore._engine().connect() as conn:  # noqa: SLF001 —— 同包内部工具
        rows = [dict(row._mapping) for row in conn.execute(DRAFTS_SQL)]
    if material:
        rows = [row for row in rows if material in (row["source_path"] or "") or row["topic"] == material]
    return rows[:limit] if limit else rows


def problems_of(row: dict) -> list[dict]:
    report = row.get("verify_report") or {}
    return list(report.get("problems") or [])


def severity_of(problem: dict) -> str:
    """优先用校验员给的 severity；老记录没有这个字段，就按 kind 兜底。

    兜底只影响"先修哪些"，不影响"修不修"——内容缺陷也允许修，但要由修复员自己
    判断该不该放弃（材料不支持就该放弃，不该硬改）。
    """
    sev = str(problem.get("severity") or "").strip()
    if sev in ("repairable", "fundamental"):
        return sev
    return "repairable" if str(problem.get("kind") or "") in MECHANICAL else "fundamental"


def body_of(raw: str) -> str:
    """去掉前置字段，只留正文。

    实测踩过：把带 front-matter 的整篇丢给修复员，它会把正文再包一层「## 题干」——
    而「题干」不是合法小节，输出直接被解析器打回。所以这里先把前置字段摘掉。
    """
    parts = raw.split("---", 2)
    return parts[2].strip() if len(parts) >= 3 else raw.strip()


def build_prompt(row: dict) -> str:
    payload = row.get("payload") or {}
    ranges = payload.get("verify_ranges") or payload.get("sources") or []
    source = Path(row["source_path"]) if row.get("source_path") else None
    if source and source.is_file() and ranges:
        window = read_ranges(str(source), ranges)
    else:
        window = "（这道题没有可用的材料窗口：只能依据它自己的解析判断，拿不准就如实放弃）"
    return render(
        load_prompt("fix"),
        {
            "QUESTION": body_of(str(row["raw_markdown"])),
            "DEFECTS": json.dumps(problems_of(row), ensure_ascii=False, indent=1),
            "SOURCE_TEXT": window,
        },
    )


async def fix_one(llm: LLM, row: dict, apply: bool) -> dict:
    reply = await llm.chat([{"role": "user", "content": build_prompt(row)}], max_tokens=4000)
    # 用量先记下来：下面的解析失败也照样花了钱，漏记会让"单位成本"失真（实测漏过一次）
    result = {"id": row["id"], "tokens": (reply.tokens_in, reply.tokens_out)}
    try:
        data = parse_json(reply.text)
    except Exception as exc:  # noqa: BLE001
        return {**result, "repairable": False, "note": f"输出不是合法 JSON：{str(exc)[:100]}", "invalid": True}
    result["repairable"] = bool(data.get("repairable"))
    result["note"] = str(data.get("note") or "")[:160]
    if not result["repairable"] or not apply:
        return result

    old = row.get("payload") or {}
    # 修复员按"只写要改的字段"回 front_matter（实测它只回 {"layer": ...}），
    # 所以这里必须用**原题的前置字段打底**再覆盖 —— 否则拼出来的题连 type 都没有，
    # 直接被解析器打回（上一轮 5/8 就是这么废掉的：指令与实现打架）。
    base = {
        key: old.get(key)
        for key in ("type", "topic", "difficulty", "layer", "wing", "chapter", "source")
        if old.get(key) not in (None, "")
    }
    front = {
        **base,
        **{k: v for k, v in (data.get("front_matter") or {}).items() if v not in (None, "")},
    }
    body = str(data.get("markdown") or "").strip()
    try:
        raw = dbstore.build_raw(front, row["id"], body)
        payload = dict(dbstore.parse_body(row["id"], raw))  # 解析器就是格式闸门
    except Exception as exc:  # noqa: BLE001 —— 输出不合契约：不算修复成功，但要记下钱的去处
        return {**result, "repairable": False, "note": f"输出不合契约：{str(exc)[:110]}", "invalid": True}
    for keep in ("sources", "verify_ranges", "point"):
        if old.get(keep) is not None:
            payload[keep] = old[keep]
    with dbstore._engine().begin() as conn:  # noqa: SLF001
        conn.execute(
            text(
                "UPDATE questions SET raw_markdown = :raw, payload = CAST(:payload AS JSONB),"
                " type = :type, topic = :topic, layer = :layer,"
                " content_hash = :hash, updated_at = now() WHERE id = :id"
            ),
            {
                "raw": raw,
                "payload": json.dumps(payload, ensure_ascii=False),
                "type": str(payload.get("type") or row["type"]),
                "topic": str(payload.get("topic") or old.get("topic") or row["topic"]),
                "layer": str(payload.get("layer") or ""),
                "hash": __import__("hashlib").sha256(raw.encode("utf-8")).hexdigest(),
                "id": row["id"],
            },
        )
    return result


async def recheck(llm: LLM, question_id: str) -> str:
    """改完立刻交**原校验员**复检 —— 修复员说改好了不算（那等于自己批自己）。

    直接调用校验函数，不走任务表：这一步是实验与验收，不是流水线的常规环节。
    """
    from .worker import run_verify  # noqa: PLC0415

    with dbstore._engine().connect() as conn:  # noqa: SLF001
        row = conn.execute(
            text(
                "SELECT q.payload, COALESCE(m.source_path, '') AS source_path"
                " FROM questions q"
                " LEFT JOIN knowledge_points kp"
                "        ON kp.key = COALESCE(NULLIF(q.payload ->> 'point',''), q.topic)"
                " LEFT JOIN materials m ON m.id = kp.material_id"
                " WHERE q.id = :id"
            ),
            {"id": question_id},
        ).fetchone()
    if row is None:
        return "?"
    payload = row[0] or {}
    source_path = str(row[1] or "")
    if not source_path or not Path(source_path).is_file():
        # 没有材料窗口就没法校验（空路径会被 Path 当成当前目录 —— 实测报过
        # IsADirectoryError: '.'）。这里如实说"无法复检"，不硬跑。
        return "?"
    result, _reply = await run_verify(
        llm,
        {
            "question_id": question_id,
            "source_path": row[1],
            "ranges": payload.get("verify_ranges") or payload.get("sources") or [],
            "prompt_version": "recheck",
        },
    )
    return str(result.get("verdict") or "?")


async def run(rows: list[dict], apply: bool, do_recheck: bool = False) -> dict:
    cfg = config.load()
    stats = {"fixed": 0, "given_up": 0, "failed": 0, "yin": 0, "yout": 0, "pass": 0, "fail": 0, "invalid": 0}
    async with LLM(cfg) as llm:
        for row in rows:
            machineish = sum(1 for p in problems_of(row) if severity_of(p) == "repairable")
            try:
                result = await fix_one(llm, row, apply)
            except Exception as exc:  # noqa: BLE001 —— 单题修复失败不该拖垮整批
                stats["failed"] += 1
                print(f"  {row['id']}  修复失败：{type(exc).__name__}: {str(exc)[:110]}")
                continue
            stats["yin"] += result["tokens"][0]
            stats["yout"] += result["tokens"][1]
            if result.get("invalid"):
                stats["invalid"] += 1
                print(f"  {row['id']}  无效输出：{result['note'][:80]}")
                continue
            if result["repairable"]:
                stats["fixed"] += 1
                line = f"  {row['id']}  已修（机械档 {machineish} 条）：{result['note'][:78]}"
                if do_recheck:
                    verdict = await recheck(llm, row["id"])
                    stats["pass" if verdict == "pass" else "fail"] += 1
                    line += f"  → 复检 {verdict}"
                print(line)
            else:
                stats["given_up"] += 1
                print(f"  {row['id']}  放弃（如实）：{result['note'][:80]}")
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.fix", description="修复被合理打回的题")
    parser.add_argument("--limit", type=int, default=0, help="最多处理几道（0=全部）")
    parser.add_argument("--apply", action="store_true", help="真的改；不加只预演")
    parser.add_argument("--recheck", action="store_true", help="改完立刻交原校验员复检")
    parser.add_argument("--material", help="只处理某个材料的草稿")
    args = parser.parse_args(argv)

    rows = drafts(args.material, args.limit)
    if not rows:
        print("没有可修的草稿（status='draft' 且有校验记录）")
        return 0
    mechanical = sum(
        1 for row in rows for p in problems_of(row) if severity_of(p) == "repairable"
    )
    total = sum(len(problems_of(row)) for row in rows)
    print(
        f"待修 {len(rows)} 道 · 缺陷 {total} 条（其中机械档 {mechanical} 条）"
        f"{'（预演）' if not args.apply else ''}"
    )
    stats = asyncio.run(run(rows, args.apply, args.recheck))
    cost = stats["yin"] / 1e6 * 2 + stats["yout"] / 1e6 * 8
    print(
        f"\n修复 {stats['fixed']} 道 · 如实放弃 {stats['given_up']} 道 · 无效输出 {stats['invalid']} 道"
        f" · 失败 {stats['failed']} 道 · 用量 {stats['yin'] / 1000:.0f}k in / {stats['yout'] / 1000:.0f}k out ≈ ¥{cost:.2f}"
    )
    if args.recheck:
        tried = stats["pass"] + stats["fail"]
        print(
            f"复检：通过 {stats['pass']} / 打回 {stats['fail']}"
            + (f" · 回收率 {100.0 * stats['pass'] / tried:.0f}%" if tried else "")
        )
    elif args.apply:
        print("下一步：加 --recheck 交原校验员复检（修复员说改好了不算）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
