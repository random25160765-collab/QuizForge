"""归并员（A2）：把各切片的候选点合成一份覆盖矩阵。

它是流水线上**唯一持有全局视图**的角色（pipeline.md §6.5），所以是串行的、单发的一步。

分工：**LLM 只做判断（分组/拆分/丢弃），字段由代码组装。**
理由很实在——让模型把 111 个点的所有字段重写一遍，输出必然撞上长度上限被截断；
而 terms / sources / slices 这些并集本来就是确定性的，交给代码更可靠、也可审计。

用法：
    api/.venv/bin/python -m pipeline.resolve --map maps/tt-metal/METALIUM_GUIDE [--force]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

import yaml

from . import config
from .llm import LLM, parse_json
from .worker import load_prompt, normalize_ranges, prompt_version, render

KINDS = {"noun", "api", "number", "invariant", "figure"}
LAYERS = {"识记", "理解", "应用", "迁移"}


def load_topic_keys() -> list[str]:
    sys.path.insert(0, str(config.ROOT / "tools"))
    import topics as _topics  # noqa: PLC0415  —— 与 build/check 共用同一份考纲解析

    nodes, _ordered, _groups, problems = _topics.load()
    for problem in problems:
        print(f"[WARN] meta/topics.yaml: {problem}", file=sys.stderr)
    return sorted(nodes)


# --------------------------------------------------------------- 组装（确定性）

def _pick(cands: list[dict], field: str, mode: str = "first"):
    values = [c.get(field) for c in cands if c.get(field)]
    if not values:
        return None
    if mode == "longest":
        return max(values, key=lambda v: len(str(v)))
    if mode == "common":
        return Counter(str(v) for v in values).most_common(1)[0][0]
    return values[0]


def _union(cands: list[dict], field: str) -> list:
    out: list = []
    for cand in cands:
        for value in cand.get(field) or []:
            if value not in out:
                out.append(value)
    return out


def assemble(candidates: list[dict], decision: dict) -> tuple[list[dict], list[dict], list[str]]:
    """把 LLM 的分组决策套回原始候选点，组装出覆盖矩阵的点列表。"""
    index: dict[str, list[dict]] = {}
    for cand in candidates:
        index.setdefault(str(cand.get("key") or ""), []).append(cand)

    warnings: list[str] = []
    used: set[str] = set()
    points: list[dict] = []
    actions: list[dict] = []

    for group in decision.get("groups") or []:
        keys = [str(k) for k in (group.get("keys") or []) if str(k) in index]
        unknown = [str(k) for k in (group.get("keys") or []) if str(k) not in index]
        if unknown:
            warnings.append(f"分组里出现未知 key（已忽略）：{unknown}")
        if not keys:
            continue
        used.update(keys)
        members = [c for k in keys for c in index[k]]
        canonical = str(group.get("canonical") or keys[0])
        layers = [layer for layer in (group.get("layers") or []) if layer in LAYERS]
        kind = group.get("kind") if group.get("kind") in KINDS else _pick(members, "kind", "common")
        points.append(
            {
                "key": canonical,
                "name": group.get("name") or _pick(members, "name", "longest"),
                "kind": kind or "noun",
                "thickness": max(
                    [int(group.get("thickness") or 0)] + [int(c.get("thickness") or 0) for c in members]
                ),
                "layers": layers or sorted(_union(members, "layers")),
                "terms": _union(members, "terms"),
                "slices": sorted({c.get("slice_id") for c in members if c.get("slice_id")}),
                "sources": [s for s in _union(members, "sources")],
                "aliases": sorted({k for k in keys if k != canonical}),
                "note": group.get("note") or _pick(members, "note", "longest"),
            }
        )
        if len(keys) > 1:
            actions.append({"type": "merge", "into": canonical, "from": sorted(set(keys) - {canonical})})

    # 没被任何分组覆盖的候选点：保留成独立点（安全网，避免丢东西）
    leftovers = [k for k in index if k not in used]
    if leftovers:
        warnings.append(f"{len(leftovers)} 个候选点没被分组覆盖，已按原样保留")
        for key in leftovers:
            members = index[key]
            points.append(
                {
                    "key": key,
                    "name": _pick(members, "name", "longest"),
                    "kind": _pick(members, "kind", "common") or "noun",
                    "thickness": max(int(c.get("thickness") or 1) for c in members),
                    "layers": sorted(_union(members, "layers")),
                    "terms": _union(members, "terms"),
                    "slices": sorted({c.get("slice_id") for c in members if c.get("slice_id")}),
                    "sources": _union(members, "sources"),
                    "aliases": [],
                    "note": _pick(members, "note", "longest"),
                }
            )

    # 安全网：同一个 canonical 被多个分组用到（模型的常见失误）→ 合并成一条。
    # key 是知识点的身份，矩阵里绝不能出现重复 key。
    deduped: dict[str, dict] = {}
    for point in points:
        current = deduped.get(point["key"])
        if current is None:
            deduped[point["key"]] = point
            continue
        current["terms"] = sorted(set(current["terms"]) | set(point["terms"]))
        current["slices"] = sorted(set(current["slices"]) | set(point["slices"]))
        current["sources"] = current["sources"] + [
            s for s in point["sources"] if s not in current["sources"]
        ]
        current["aliases"] = sorted(set(current["aliases"]) | set(point["aliases"]))
        current["layers"] = sorted(set(current["layers"]) | set(point["layers"]))
        current["thickness"] = max(current["thickness"], point["thickness"])
        if len(str(point.get("name") or "")) > len(str(current.get("name") or "")):
            current["name"] = point["name"]
        if len(str(point.get("note") or "")) > len(str(current.get("note") or "")):
            current["note"] = point["note"]
        warnings.append(f"canonical「{point['key']}」被多个分组重复使用，已自动合并")
    points = list(deduped.values())

    # 拆分：把父点换成若干子点，出处/切片继承父点
    by_key = {p["key"]: p for p in points}
    for split in decision.get("splits") or []:
        parent_key = str(split.get("key") or "")
        parent = by_key.get(parent_key)
        if parent is None:
            warnings.append(f"拆分里出现未知 key（已忽略）：{parent_key}")
            continue
        children = split.get("into") or []
        if not children:
            continue
        points.remove(parent)
        for child in children:
            layers = [layer for layer in (child.get("layers") or parent["layers"]) if layer in LAYERS]
            points.append(
                {
                    "key": str(child.get("key")),
                    "name": child.get("name"),
                    "kind": child.get("kind") if child.get("kind") in KINDS else parent["kind"],
                    "thickness": int(child.get("thickness") or max(1, parent["thickness"] - 1)),
                    "layers": layers or parent["layers"],
                    "terms": child.get("terms") or parent["terms"],
                    "slices": parent["slices"],
                    "sources": parent["sources"],
                    "aliases": [],
                    "note": child.get("note") or parent["note"],
                }
            )
        actions.append({"type": "split", "from": parent_key, "into": [c.get("key") for c in children]})

    # 丢弃
    for item in decision.get("drop") or []:
        key = str(item.get("key") or "")
        hit = next((p for p in points if p["key"] == key or key in p.get("aliases", [])), None)
        if hit:
            points.remove(hit)
            actions.append({"type": "drop", "key": key, "why": item.get("why")})
        else:
            warnings.append(f"丢弃里出现未知 key（已忽略）：{key}")

    points.sort(key=lambda p: ((p.get("slices") or [""])[0], p["key"]))
    return points, actions, warnings


# --------------------------------------------------------------- 主流程

def candidates_from_db(slug: str) -> tuple[list[dict], int]:
    """库里该材料的点候选（含各自切片的行数），形状与 extract/*.json 一致。

    候选分两类，**同一张表、同一条归并路径**：文字抽取的（`origin=text`）与
    读图得到的事实（`origin=figure`）。图和文字在这里汇合，于是"图支持的点"
    与"文字支持的点"会被合并成同一个点，而不是各记一份。

    `sources` 统一用 `normalize_ranges` 收成区间列表 —— 抽取员给的是单数 `source`，
    库里存的是标准化之后的 `sources`，归并只要面对一种形状。
    """
    import json as _json  # noqa: PLC0415

    from sqlalchemy import text  # noqa: PLC0415

    from app.db import get_engine  # noqa: PLC0415

    with get_engine().connect() as conn:
        rows = conn.execute(
            text(
                "SELECT pc.slice_id, pc.key, pc.name, pc.kind, pc.thickness, pc.layers,"
                " pc.sources, pc.terms, pc.raw FROM point_candidates pc"
                " JOIN materials m ON m.id = pc.material_id"
                " WHERE m.slug = :slug ORDER BY pc.id"
            ),
            {"slug": slug},
        ).fetchall()
        lines = conn.execute(
            text(
                "SELECT COALESCE(SUM(s.end_line - s.start_line + 1), 0) FROM material_slices s"
                " JOIN materials m ON m.id = s.material_id WHERE m.slug = :slug"
            ),
            {"slug": slug},
        ).scalar()

    out: list[dict] = []
    for row in rows:
        data = dict(row._mapping)
        raw = data.get("raw")
        if isinstance(raw, str):
            try:
                raw = _json.loads(raw)
            except ValueError:
                raw = {}
        origin = str((raw or {}).get("origin") or "text")
        out.append(
            {
                "slice_id": data["slice_id"],
                "key": data["key"],
                "name": data["name"],
                "kind": data["kind"],
                "thickness": data["thickness"],
                "layers": data["layers"] or [],
                "terms": data["terms"] or [],
                "sources": normalize_ranges(data["sources"]),
                "origin": origin,
            }
        )
    return out, int(lines or 0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.resolve", description="归并成覆盖矩阵")
    parser.add_argument("--map", required=True)
    parser.add_argument("--force", action="store_true", help="输入没变也重跑")
    args = parser.parse_args(argv)

    map_dir = Path(args.map) if Path(args.map).is_absolute() else config.ROOT / args.map
    # 候选点从**库**里取（`point_candidates`）。抽取结果落库之后，`maps/` 只是可重建的
    # 中间产物，归并这一步不该再依赖它 —— 这是"上下游彻底对齐"的关键一刀。
    slug = map_dir.relative_to(config.MAPS_DIR).as_posix()
    candidates, db_lines = candidates_from_db(slug)
    total_lines = db_lines
    if not candidates:
        # 兜底：还没同步进库的历史材料，仍然按文件读
        extract_dir = map_dir / "extract"
        files = sorted(extract_dir.glob("*.json"))
        if not files:
            raise SystemExit(f"库里没有候选点（{slug}），也没有抽取结果：{extract_dir}")
        for path in files:
            data = json.loads(path.read_text(encoding="utf-8"))
            loc = (data.get("loc") or {}).get("lines") or [0, 0]
            total_lines += max(0, int(loc[1]) - int(loc[0]) + 1)
            for point in data.get("points") or []:
                cand = {"slice_id": data["slice_id"], **point}
                # 抽取员给的是单数 source（一个行区间），归并统一成复数 sources（区间列表）。
                # 之前这两个名字对不上，矩阵里的出处全成了空数组 ——
                # 出题员拿不到原文、校验员更拿不到，于是整批题被以「原文未提供」打回。
                cand["sources"] = normalize_ranges(cand.get("sources") or cand.get("source"))
                candidates.append(cand)
    # 视觉员产出的事实也进同一个候选池：图给的是**空间关系**（谁连谁、箭头朝哪），
    # 这些往往正文根本没写全，所以图本身就是一个知识点。
    # 每条 fact 按它声明的 key 变成候选点，出处取图所在行 —— 与文字候选走同一条归并路径，
    # 于是「图支持的点」与「文字支持的点」会被合并成同一个点（而不是各记一份）。
    vision_dir = map_dir / "figures"
    # 图事实已经随文字候选一起从库里来了（`origin=figure`）—— 这时不再读文件，
    # 否则同一个事实会被算两遍。文件路径只留给还没同步进库的历史材料。
    vision_files = (
        []
        if any(cand.get("origin") == "figure" for cand in candidates)
        else (sorted(vision_dir.glob("*.json")) if vision_dir.is_dir() else [])
    )
    figure_candidates = 0
    for path in vision_files:
        data = json.loads(path.read_text(encoding="utf-8"))
        result = data.get("result") or {}
        ranges = normalize_ranges([(data.get("loc") or {}).get("lines")])
        for fact in result.get("facts") or []:
            key = str(fact.get("key") or "").strip()
            if not key:
                continue
            candidates.append(
                {
                    "slice_id": data.get("figure_id"),
                    "key": key,
                    "name": fact.get("statement") or key,
                    "kind": fact.get("kind") or "figure",
                    "thickness": 2,
                    "layers": ["识记", "理解"],
                    "terms": [],
                    "sources": ranges,
                    "origin": "figure",
                }
            )
            figure_candidates += 1
    if figure_candidates:
        print(f"并入 {figure_candidates} 条图事实（来自 {vision_dir.name}/）")

    topic_keys = load_topic_keys()

    # 粒度先验：每 ~20 行正文一个知识点。它不精确，但能把「粒度」从玄学变成可核对的目标
    # —— 实测没有这个约束时，同一份材料一次归并出 15 个点、下一次出 50 个点。
    target = max(8, round(total_lines / 20))
    target_range = f"{round(target * 0.7)}–{round(target * 1.3)}"

    prompt_ver = prompt_version("resolve")
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.read_bytes())
    digest.update(prompt_ver.encode())
    digest.update("\n".join(topic_keys).encode("utf-8"))
    input_hash = digest.hexdigest()[:16]

    out_file = map_dir / "coverage.yaml"
    if out_file.is_file() and not args.force:
        head = yaml.safe_load(out_file.read_text(encoding="utf-8")) or {}
        if head.get("input_hash") == input_hash:
            print(f"输入未变（hash={input_hash}），跳过。要重跑加 --force。")
            return 0

    # 只喂给模型判断够用的字段，字段本身由代码组装
    compact = [
        {
            "key": c.get("key"),
            "name": c.get("name"),
            "kind": c.get("kind"),
            "thickness": c.get("thickness"),
            "slices": [c.get("slice_id")],
        }
        for c in candidates
    ]
    prompt = render(
        load_prompt("resolve"),
        {
            "TOPICS_KEYS": "\n".join(f"- {k}" for k in topic_keys),
            "CANDIDATES": json.dumps(compact, ensure_ascii=False, indent=1),
            "TOTAL_LINES": str(total_lines),
            "TARGET": target_range,
        },
    )
    print(f"归并 {len(candidates)} 个候选点（{len(files)} 个切片，{len(topic_keys)} 个考纲 key）…")

    cfg = config.load()

    async def run() -> dict:
        async with LLM(cfg) as llm:
            reply = await llm.chat([{"role": "user", "content": prompt}], max_tokens=8000)
            print(f"model={reply.model} in={reply.tokens_in} out={reply.tokens_out}")
            return {"decision": parse_json(reply.text), "reply": reply}

    result = asyncio.run(run())
    points, actions, warnings = assemble(candidates, result["decision"])
    missing = result["decision"].get("topics_missing") or []

    payload = {
        "material": map_dir.name,
        "generated_by": {"model": result["reply"].model, "prompt_version": prompt_ver},
        "input_hash": input_hash,
        "stats": {
            "candidates": len(candidates),
            "points": len(points),
            "merged": len([a for a in actions if a["type"] == "merge"]),
            "split": len([a for a in actions if a["type"] == "split"]),
            "dropped": len([a for a in actions if a["type"] == "drop"]),
            "topics_missing": len(missing),
        },
        "points": points,
        "topics_missing": missing,
        "actions": actions,
    }
    out_file.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, width=110),
        encoding="utf-8",
    )

    stats = payload["stats"]
    print(f"\n写入 {out_file}")
    print(
        f"  {stats['candidates']} 个候选 → {stats['points']} 个知识点"
        f"（合并 {stats['merged']}、拆分 {stats['split']}、丢弃 {stats['dropped']}）"
    )
    for warning in warnings:
        print(f"  [WARN] {warning}", file=sys.stderr)
    if missing:
        print("  需要改考纲的 key：")
        for item in missing:
            print(f"    - {item.get('key')}：{item.get('name')}（挂 {item.get('parent_hint')}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
