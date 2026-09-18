"""覆盖率对账：**哪些材料出过题、出到什么程度、还剩哪些点**。

为什么需要它：材料一多（tt-metal 一个仓库就有 59 份技术报告），最怕重复出题 ——
同一个知识点反复考，既白花钱，又让题库里堆满近似题。这张表回答三件事：

* 每份材料出了多少题、还有多少个点没出；
* 哪些材料的点已经**出满**（派工会自动跳过它们；要重出必须显式重建任务）；
* 全库的整体覆盖率 —— 与 `dispatch` 的"只补缺口"是**同一套判据**，不能两套。

判据两条，取并集：

① **题↔点的边**（`question_points`）—— 精确；
② 题目的 `topic` 等于点 key —— 点 key 进了考纲（补成叶子）之后这条很可靠：
   那一步的意义正是让"点 key"成为合法考点，两套命名空间在这里对齐。

最后还有一条**硬断言**：**游离题（现行题里没挂概念的）必须为零**，
否则退出码非零 —— 它们对图谱不可见，覆盖率 / 选题 / 掌握度会一起漏掉它们。

用法：

    python -m pipeline.coverage                     # 全部材料，按缺口排序
    python -m pipeline.coverage --material ViT-TTNN-vit_bh
    python -m pipeline.coverage --material X --gaps # 只列还没出题的点
"""

from __future__ import annotations

import argparse
import json
import sys

from sqlalchemy import text

from . import config

sys.path.insert(0, str(config.ROOT / "api"))

# 一个材料里：点数 / 已出题的点数 / 已发布题数 / 草稿题数
SUMMARY_SQL = text(
    """
    WITH pts AS (
      SELECT material_id, key FROM knowledge_points
    ),
    linked AS (  -- ① 题↔点的边
      SELECT kp.material_id, kp.key
      FROM question_points qp JOIN knowledge_points kp ON kp.id = qp.point_id
      UNION
      SELECT kp.material_id, kp.key  -- ② 题的 topic 直接等于点 key
      FROM knowledge_points kp
      JOIN questions q ON q.topic = kp.key AND q.status IN ('published', 'verified')
    ),
    qs AS (
      SELECT kp.material_id,
             COUNT(DISTINCT q.id) FILTER (WHERE q.status = 'published') AS published,
             COUNT(DISTINCT q.id) FILTER (WHERE q.status = 'draft') AS drafts
      FROM questions q JOIN knowledge_points kp ON kp.key = q.topic
      GROUP BY kp.material_id
    )
    SELECT m.slug,
           m.subject,
           COUNT(pts.key) AS points,
           COUNT(DISTINCT l.key) AS covered,
           COALESCE(MAX(qs.published), 0) AS published,
           COALESCE(MAX(qs.drafts), 0) AS drafts
    FROM materials m
    LEFT JOIN pts ON pts.material_id = m.id
    LEFT JOIN linked l ON l.material_id = m.id AND l.key = pts.key
    LEFT JOIN qs ON qs.material_id = m.id
    GROUP BY m.slug, m.subject
    HAVING COUNT(pts.key) > 0
    ORDER BY (COUNT(pts.key) - COUNT(DISTINCT l.key)) DESC, m.slug
    """
)

GAPS_SQL = text(
    """
    WITH linked AS (
      SELECT kp.material_id, kp.key
      FROM question_points qp JOIN knowledge_points kp ON kp.id = qp.point_id
      UNION
      SELECT kp.material_id, kp.key
      FROM knowledge_points kp
      JOIN questions q ON q.topic = kp.key AND q.status IN ('published', 'verified')
    )
    SELECT kp.key, kp.name, kp.thickness, kp.producible
    FROM knowledge_points kp
    JOIN materials m ON m.id = kp.material_id
    WHERE m.slug = :slug
      AND NOT EXISTS (SELECT 1 FROM linked l WHERE l.material_id = kp.material_id AND l.key = kp.key)
    ORDER BY kp.key
    """
)


ORPHAN_SQL = text(
    """
    SELECT q.id, q.topic, q.layer, q.payload
    FROM questions q
    WHERE q.retired_at IS NULL
      AND q.status IN ('published', 'verified')
      AND NOT EXISTS (SELECT 1 FROM question_concepts qc WHERE qc.question_id = q.id)
    ORDER BY q.id
    """
)


def _engine():
    from app.db import get_engine  # noqa: PLC0415

    return get_engine()


def orphans(limit: int = 0) -> list[dict]:
    """**游离题**：现行题里一条概念边都没有的。

    为什么它是硬断言而不是"看一眼"：没挂概念的题对图谱是**不可见**的 ——
    覆盖率算不到它、按知识点选题选不到它、掌握度也回流不到图上。它不报错，
    只是安静地掉出所有算法（实测曾经有 86 道）。所以对账里必须让它为零。

    `payload` 在应用层切片（不用 `->>`）：B 段要换内置引擎，SQL 里的 JSON 运算符
    是最容易被方言绊住的一处。
    """
    out: list[dict] = []
    with _engine().connect() as conn:
        for row in conn.execute(ORPHAN_SQL):
            payload = row[3]
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except ValueError:
                    payload = {}
            stem = str((payload or {}).get("stem") or "").replace("\n", " ")
            out.append(
                {
                    "id": row[0],
                    "topic": row[1] or "",
                    "layer": row[2] or "",
                    "stem": stem[:40],
                }
            )
    return out[:limit] if limit else out


def rows_for(material: str | None) -> list[dict]:
    with _engine().connect() as conn:
        data = [dict(row._mapping) for row in conn.execute(SUMMARY_SQL)]
    if material:
        data = [row for row in data if row["slug"] == material]
    return data


def verdict(row: dict) -> str:
    if row["covered"] >= row["points"]:
        return "已出满"
    if row["covered"] == 0:
        return "未出题"
    return "可继续"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.coverage", description="覆盖率对账")
    parser.add_argument("--material", help="只看某个材料（slug）")
    parser.add_argument("--gaps", action="store_true", help="只列还没出题的点（需配合 --material）")
    parser.add_argument("--limit", type=int, default=0, help="最多显示多少行")
    args = parser.parse_args(argv)

    if args.gaps and not args.material:
        raise SystemExit("--gaps 需要配合 --material 指定材料")

    if args.gaps:
        with _engine().connect() as conn:
            gaps = [dict(row._mapping) for row in conn.execute(GAPS_SQL, {"slug": args.material})]
        print(f"{args.material} 还没出题的点：{len(gaps)} 个")
        for gap in gaps:
            flag = "" if gap["producible"] else "  ⟵ 材料太薄，撑不起题"
            print("  %-46s 厚度 %d%s" % (gap["key"], gap["thickness"], flag))
        return 0

    data = rows_for(args.material)
    if not data:
        print("没有数据（材料库里没有这些点，或 slug 写错了）")
        return 0
    if args.limit:
        data = data[: args.limit]

    total_points = sum(row["points"] for row in data)
    total_covered = sum(row["covered"] for row in data)
    print(
        f"{'材料':<46} {'点':>4} {'已出':>4} {'覆盖':>5} {'已发布':>5} {'草稿':>4}  结论"
    )
    for row in data:
        ratio = 100.0 * row["covered"] / row["points"] if row["points"] else 0.0
        print(
            "%-46s %4d %4d %4.0f%% %5d %4d  %s"
            % (
                row["slug"][:46],
                row["points"],
                row["covered"],
                ratio,
                row["published"],
                row["drafts"],
                verdict(row),
            )
        )
    print()
    print(
        "判据：① 题↔点的边（精确）② 题的 topic 等于点 key（点 key 补进考纲后可靠）。\n"
        "      历史那批题若挂在**单元级** topic 上，会少算 —— 所以「已出题的点」是下限，"
        "不是上限。"
    )
    done = sum(1 for row in data if row["covered"] >= row["points"])
    print(
        "合计：材料 %d · 点 %d · 已出题的点 %d（%.0f%%）· 材料已出满 %d · 完全没出过题 %d"
        % (
            len(data),
            total_points,
            total_covered,
            100.0 * total_covered / total_points if total_points else 0.0,
            done,
            sum(1 for row in data if row["covered"] == 0),
        )
    )

    # 硬断言：游离题必须为零（详见 `orphans`）
    stray = orphans()
    print()
    print(f"游离题（现行题里没挂概念）：{len(stray)} 道")
    if stray:
        for item in stray[:10]:
            print("  %-16s %-6s %s" % (item["id"], item["layer"] or "-", item["stem"]))
        if len(stray) > 10:
            print(f"  …… 另有 {len(stray) - 10} 道")
        print(
            "  ⟵ 它们对图谱不可见（覆盖率 / 选题 / 掌握度全漏）。\n"
            "     补法：`python -m pipeline.graph_build merge`（按题↔点回填概念），"
            "再不到位就得人工挂。"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
