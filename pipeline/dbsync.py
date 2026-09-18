"""把知识空间灌进 PostgreSQL（幂等，可反复跑）。

为什么要有这一步：题目与知识点一多，靠 `maps/` 目录和 YAML **选不动也查不动** ——
后续做题要靠算法在题目空间里检索，那必须有库。于是分工改成：

    文件  = 导入导出格式（可重建、可审计、可 diff）
    数据库 = 检索与调度的权威

灌进去的四样（对应 2.0 双数据库里的「知识空间」）：

* `materials`        材料（一份源文件）
* `knowledge_points` 点（可考知识点，出题的原料单位）
* `point_sources`    出处（行区间，一切论断可追溯的地基）
* `point_edges`      带类型的边（图就在这里，不需要独立的图数据库）
* `question_points`  题 ↔ 点（题目空间与知识空间的**唯一**连接）

边的来源分成两类：`requires` 要靠 LLM 判（现在还没做，留空）；
其余三种都能从已有数据**确定性推出** —— `same_section`（点的 slices 交集）、
`shares_terms`（术语交集 ≥ 3）、`part_of`（归并阶段的 split 动作）。

用法（先 `import_bank`，否则题 ↔ 点的连接会因外键缺失被跳过）：
    api/.venv/bin/python -m pipeline.dbsync                 # 全量
    api/.venv/bin/python -m pipeline.dbsync --material LLMs-llms
"""

from __future__ import annotations

import argparse
import json
import sys
from itertools import combinations
from pathlib import Path

import yaml
from sqlalchemy import text

from . import config

# pipeline 与 api 是两个包，连接层复用 api/app/db.py（惰性引擎，配置同源）
sys.path.insert(0, str(config.ROOT / "api"))

UPSERT_MATERIAL = text(
    """
    INSERT INTO materials (slug, subject, title, source_path, sha256, lines, updated_at)
    VALUES (:slug, :subject, :title, :source_path, :sha256, :lines, CURRENT_TIMESTAMP)
    ON CONFLICT (slug) DO UPDATE SET
      subject = EXCLUDED.subject, title = EXCLUDED.title,
      source_path = EXCLUDED.source_path, sha256 = EXCLUDED.sha256,
      lines = EXCLUDED.lines, updated_at = CURRENT_TIMESTAMP
    RETURNING id
    """
)
UPSERT_POINT = text(
    """
    INSERT INTO knowledge_points
      (material_id, key, name, kind, thickness, layers, producible, note)
    VALUES
      (:material_id, :key, :name, :kind, :thickness, :layers, :producible, :note)
    ON CONFLICT (material_id, key) DO UPDATE SET
      name = EXCLUDED.name, kind = EXCLUDED.kind, thickness = EXCLUDED.thickness,
      layers = EXCLUDED.layers, producible = EXCLUDED.producible, note = EXCLUDED.note
    RETURNING id
    """
)
INSERT_SOURCE = text(
    """
    INSERT INTO point_sources (point_id, slice_id, start_line, end_line)
    VALUES (:point_id, :slice_id, :start_line, :end_line)
    ON CONFLICT (point_id, start_line, end_line) DO NOTHING
    """
)
INSERT_EDGE = text(
    """
    INSERT INTO point_edges (from_point_id, to_point_id, type, why, derived_by)
    VALUES (:a, :b, :type, :why, :derived_by)
    ON CONFLICT (from_point_id, to_point_id, type) DO NOTHING
    """
)
INSERT_LINK = text(
    """
    INSERT INTO question_points (question_id, point_id, is_primary)
    VALUES (:question_id, :point_id, true)
    ON CONFLICT (question_id, point_id) DO NOTHING
    """
)


UPSERT_SLICE = text(
    """
    INSERT INTO material_slices (material_id, slice_id, path, start_line, end_line, tokens,
                                 figures, summary)
    VALUES (:material_id, :slice_id, :path, :start, :end, :tokens,
            :figures, :summary)
    ON CONFLICT (material_id, slice_id) DO UPDATE SET
      path = EXCLUDED.path, start_line = EXCLUDED.start_line, end_line = EXCLUDED.end_line,
      tokens = EXCLUDED.tokens, figures = EXCLUDED.figures, summary = EXCLUDED.summary
    """
)

INSERT_CANDIDATE = text(
    """
    INSERT INTO point_candidates (material_id, slice_id, key, name, kind, thickness, layers,
                                  sources, terms, raw, consumed)
    VALUES (:material_id, :slice_id, :key, :name, :kind, :thickness, :layers,
            :sources, :terms, :raw, false)
    ON CONFLICT (material_id, slice_id, key) DO NOTHING
    """
)


def sync_material(conn, map_dir: Path) -> dict:
    """把一个材料的 material.yaml + slices.yaml + coverage.yaml 灌进库。"""
    stats = {"points": 0, "sources": 0, "edges": 0, "slices": 0, "candidates": 0, "figures": 0}
    meta_file = map_dir / "material.yaml"
    coverage_file = map_dir / "coverage.yaml"
    slices_file_early = map_dir / "slices.yaml"
    if not coverage_file.is_file():
        return stats

    # 早期材料（试点那批）只有 slices.yaml，没有 material.yaml —— 不强求：
    # 身份与源文件路径从 slices.yaml 取，slug 用目录相对路径。
    # 没有这一段，那批材料就永远进不了库，`maps/` 也就永远删不掉。
    if meta_file.is_file():
        meta = yaml.safe_load(meta_file.read_text(encoding="utf-8")) or {}
    elif slices_file_early.is_file():
        early = yaml.safe_load(slices_file_early.read_text(encoding="utf-8")) or {}
        meta = {
            "material": map_dir.relative_to(config.MAPS_DIR).as_posix(),
            "source_path": early.get("source_path") or "",
            "subject": map_dir.parent.name,
        }
    else:
        return stats
    coverage = yaml.safe_load(coverage_file.read_text(encoding="utf-8")) or {}
    material_id = conn.execute(
        UPSERT_MATERIAL,
        {
            "slug": meta.get("material") or map_dir.name,
            "subject": meta.get("subject") or "",
            "title": str(meta.get("title") or ""),
            "source_path": str(meta.get("source_path") or ""),
            "sha256": str(meta.get("sha256") or ""),
            "lines": int(meta.get("lines") or 0),
        },
    ).scalar_one()

    # 切片与点候选也入库：派工要问"哪些切片还没抽过"、归并要问"抽到的候选点是什么"，
    # 这两个问题在几十个材料、上千个切片之后，靠扫 maps/ 下的文件不成立。
    # 入库之后 `maps/` 只是可重建的中间产物，不再是权威。
    slices_file = map_dir / "slices.yaml"
    if slices_file.is_file():
        doc = yaml.safe_load(slices_file.read_text(encoding="utf-8")) or {}
        conn.execute(
            text("DELETE FROM material_slices WHERE material_id = :mid"), {"mid": material_id}
        )
        for item in doc.get("slices") or []:
            lines = (item.get("loc") or {}).get("lines") or [0, 0]
            # `path` 有两种写法：列表（[Executive Summary]）与字符串（"x.md#L1-L84"）。
            # 直接 list() 会把字符串拆成逐字符数组（实测踩过），这里统一成列表。
            raw_path = item.get("path")
            path_value = [raw_path] if isinstance(raw_path, str) else list(raw_path or [])
            conn.execute(
                UPSERT_SLICE,
                {
                    "material_id": material_id,
                    "slice_id": str(item.get("id") or ""),
                    "path": json.dumps(path_value, ensure_ascii=False),
                    "start": int(lines[0]) if lines else 0,
                    "end": int(lines[-1]) if lines else 0,
                    "tokens": str(item.get("tokens") or ""),
                    "figures": json.dumps(list(item.get("figures") or []), ensure_ascii=False),
                    "summary": str(item.get("summary") or ""),
                },
            )
            stats["slices"] += 1

    extract_dir = map_dir / "extract"
    if extract_dir.is_dir():
        conn.execute(
            text("DELETE FROM point_candidates WHERE material_id = :mid"), {"mid": material_id}
        )
        for extract_path in sorted(extract_dir.glob("*.json")):
            data = json.loads(extract_path.read_text(encoding="utf-8"))
            for candidate in data.get("points") or []:
                ckey = str(candidate.get("key") or "").strip()
                if not ckey:
                    continue
                sources = candidate.get("sources")
                if not sources and candidate.get("source"):
                    sources = [candidate["source"]]
                conn.execute(
                    INSERT_CANDIDATE,
                    {
                        "material_id": material_id,
                        "slice_id": str(data.get("slice_id") or extract_path.stem),
                        "key": ckey,
                        "name": str(candidate.get("name") or ""),
                        "kind": str(candidate.get("kind") or "noun"),
                        "thickness": int(candidate.get("thickness") or 1),
                        "layers": json.dumps(list(candidate.get("layers") or []), ensure_ascii=False),
                        "sources": json.dumps(sources or [], ensure_ascii=False),
                        "terms": json.dumps(list(candidate.get("terms") or []), ensure_ascii=False),
                        "raw": json.dumps(candidate, ensure_ascii=False),
                    },
                )
                stats["candidates"] += 1

    # 图清单与读图事实也入库：图是这条流水线的一等公民（正文用"The following image…"
    # 把图当解释主体），派工要问"还有哪些图没读过"，归并要拿"图读出了什么"。
    figures_file = map_dir / "figures.yaml"
    if figures_file.is_file():
        figures_doc = yaml.safe_load(figures_file.read_text(encoding="utf-8")) or {}
        conn.execute(
            text("DELETE FROM material_figures WHERE material_id = :mid"), {"mid": material_id}
        )
        for figure in figures_doc.get("figures") or []:
            fig_lines = (figure.get("loc") or {}).get("lines") or [0, 0]
            figure_id = str(figure.get("id") or "")
            if not figure_id:
                continue
            conn.execute(
                text(
                    "INSERT INTO material_figures (material_id, figure_id, src, slice_id, start_line,"
                    " end_line, caption, kind, keys, plan)"
                    " VALUES (:mid, :fid, :src, :sid, :start, :end, :caption, :kind,"
                    " :keys, :plan)"
                    " ON CONFLICT (material_id, figure_id) DO UPDATE SET"
                    " src = EXCLUDED.src, slice_id = EXCLUDED.slice_id,"
                    " start_line = EXCLUDED.start_line, end_line = EXCLUDED.end_line,"
                    " caption = EXCLUDED.caption, kind = EXCLUDED.kind, keys = EXCLUDED.keys,"
                    " plan = EXCLUDED.plan"
                ),
                {
                    "mid": material_id,
                    "fid": figure_id,
                    "src": str(figure.get("src") or ""),
                    "sid": str(figure.get("slice") or ""),
                    "start": int(fig_lines[0]) if fig_lines else 0,
                    "end": int(fig_lines[-1]) if fig_lines else 0,
                    "caption": str(figure.get("caption") or figure.get("title") or ""),
                    "kind": str(figure.get("kind") or ""),
                    "keys": json.dumps(list(figure.get("keys") or []), ensure_ascii=False),
                    "plan": str(figure.get("plan") or ""),
                },
            )
            stats["figures"] += 1

    vision_dir = map_dir / "figures"
    if vision_dir.is_dir():
        for vision_path in sorted(vision_dir.glob("*.json")):
            data = json.loads(vision_path.read_text(encoding="utf-8"))
            result = data.get("result") or {}
            v_lines = (data.get("loc") or {}).get("lines") or [0, 0]
            figure_id = str(data.get("figure_id") or vision_path.stem)
            conn.execute(
                text("DELETE FROM point_candidates WHERE material_id = :mid AND slice_id = :sid"),
                {"mid": material_id, "sid": figure_id},
            )
            for fact in result.get("facts") or []:
                fkey = str(fact.get("key") or "").strip()
                if not fkey:
                    continue
                conn.execute(
                    INSERT_CANDIDATE,
                    {
                        "material_id": material_id,
                        "slice_id": figure_id,
                        "key": fkey,
                        "name": str(fact.get("statement") or fkey),
                        "kind": str(fact.get("kind") or "figure"),
                        "thickness": 2,
                        "layers": json.dumps(["识记", "理解"], ensure_ascii=False),
                        "sources": json.dumps([v_lines], ensure_ascii=False),
                        "terms": json.dumps([], ensure_ascii=False),
                        "raw": json.dumps({"origin": "figure", **fact}, ensure_ascii=False),
                    },
                )
                stats["candidates"] += 1

    key_to_point: dict[str, int] = {}
    slice_of: dict[str, list[str]] = {}
    terms_of: dict[str, set[str]] = {}
    for point in coverage.get("points") or []:
        key = str(point.get("key") or "")
        if not key:
            continue
        pid = conn.execute(
            UPSERT_POINT,
            {
                "material_id": material_id,
                "key": key,
                "name": str(point.get("name") or ""),
                "kind": str(point.get("kind") or "noun"),
                "thickness": int(point.get("thickness") or 1),
                # 可考性验收：厚度为 0 或没有出处的点标记为不可出题
                "producible": bool(point.get("sources")) and int(point.get("thickness") or 1) > 0,
                "layers": json.dumps(list(point.get("layers") or []), ensure_ascii=False),
                "note": str(point.get("note") or ""),
            },
        ).scalar_one()
        key_to_point[key] = pid
        slice_of[key] = [str(s) for s in (point.get("slices") or [])]
        terms_of[key] = {str(t) for t in (point.get("terms") or [])}
        stats["points"] += 1
        for rng in point.get("sources") or []:
            if not isinstance(rng, (list, tuple)) or len(rng) != 2:
                continue
            conn.execute(
                INSERT_SOURCE,
                {
                    "point_id": pid,
                    "slice_id": (slice_of[key] or [""])[0],
                    "start_line": int(rng[0]),
                    "end_line": int(rng[1]),
                },
            )
            stats["sources"] += 1

    # 确定性边：同一节 / 共享术语。两两组合，方向按 (小 id → 大 id) 固定，
    # 这样唯一约束能挡住重复（同一条边不该因为方向不同存两次）。
    def add_edge(a_key: str, b_key: str, kind: str, why: str) -> None:
        a, b = key_to_point.get(a_key), key_to_point.get(b_key)
        if not a or not b or a == b:
            return
        lo, hi = sorted((a, b))
        conn.execute(INSERT_EDGE, {"a": lo, "b": hi, "type": kind, "why": why, "derived_by": "code"})
        stats["edges"] += 1

    for left, right in combinations(sorted(slice_of), 2):
        shared = set(slice_of[left]) & set(slice_of[right])
        if shared:
            add_edge(left, right, "same_section", f"同切片 {', '.join(sorted(shared)[:3])}")
        common = terms_of[left] & terms_of[right]
        if len(common) >= 3:
            add_edge(left, right, "shares_terms", f"共享术语 {', '.join(sorted(common)[:5])}")

    # 拆分关系（part_of）：归并阶段的 split 动作里带着父 → 子
    for action in coverage.get("actions") or []:
        if action.get("type") != "split":
            continue
        for child in action.get("into") or []:
            add_edge(str(child), str(action.get("from")), "part_of", "归并阶段拆分")
    return stats


def link_questions(conn) -> tuple[int, int]:
    """题 ↔ 点：按 question.topic 与 point.key 对上。

    这是个**近似**：点 key 与考纲 topic 是两套命名空间，能对上的先连上，
    对不上的计入 unmatched（下一版由出题时直接写 `points` 字段来根治）。
    """
    linked = unmatched = 0
    rows = conn.execute(
        text(
            "SELECT q.id, q.topic FROM questions q "
            "WHERE q.retired_at IS NULL AND q.topic IS NOT NULL AND q.topic <> ''"
        )
    ).fetchall()
    for qid, topic in rows:
        with_id, without_id = topic, topic
        hit = conn.execute(
            text("SELECT id FROM knowledge_points WHERE key = :key"), {"key": with_id}
        ).fetchall()
        if not hit:
            unmatched += 1
            continue
        for (pid,) in hit:
            conn.execute(INSERT_LINK, {"question_id": qid, "point_id": pid})
            linked += 1
    return linked, unmatched


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.dbsync", description="知识空间入库")
    parser.add_argument("--material", help="只同步某个材料（目录名）")
    args = parser.parse_args(argv)

    from app.db import get_engine  # noqa: PLC0415 —— 惰性引擎，配置与 api 同源

    total = {"points": 0, "sources": 0, "edges": 0}
    dirs = sorted(p for p in config.MAPS_DIR.glob("*/*") if p.is_dir())
    if args.material:
        dirs = [p for p in dirs if p.name == args.material]
    if not dirs:
        raise SystemExit(
            "没有找到材料目录（maps/<subject>/<material>/）。\n"
            "注意：知识空间的权威已经在数据库 —— 这个命令只是**把历史的 maps/ 搬进库**，\n"
            "maps/ 删掉之后它就没有活可干了（新产出直接入库，见 pipeline/dbstore.py）。"
        )

    with get_engine().begin() as conn:
        for map_dir in dirs:
            stats = sync_material(conn, map_dir)
            if stats["points"]:
                print(
                    f"  {map_dir.name:56s} {stats['points']:3d} 点 · "
                    f"{stats['sources']:3d} 处出处 · {stats['edges']:4d} 条边 · "
                    f"{stats['slices']:2d} 切片 · {stats['candidates']:3d} 候选"
                )
            for key in total:
                total[key] += stats[key]
        linked, unmatched = link_questions(conn)

    print(
        f"\n入库：{total['points']} 个知识点 · {total['sources']} 处出处 · {total['edges']} 条边 · "
        f"题↔点连接 {linked} 条（topic 对不上的题 {unmatched} 道）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
