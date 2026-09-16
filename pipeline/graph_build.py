"""把知识空间从"点清单"变成**知识图谱**。

问题出在哪（数据说话）：库里 981 个点全部 material-scoped ——
`tt-metal-circular-buffer` 在 10 份材料里就是 10 个互不相识的点；
边 4841 条里 4553 条是 `same_section`、288 条 `shares_terms`，**全部由代码按字面算出**，
没有一条语义关系。所以"图谱"其实是每份材料内部的段落聚类，
主题树只能逐个点镜像出叶子（tt-metal 下 443 片），题目只能绑到"某材料某段"。

三步把它扳回来：

* ``merge`` —— 981 个点归并成概念：点退化为"概念在某份材料里的一次出现"。
* ``edges`` —— 派**程序能确定派生**的边：同切片共现（co_occurs，弱边，当证据）。
* ``relate`` —— 让模型给反复共现的概念对判**语义关系**（requires / part_of /
  contrast_with / implements）。这一步才是"知识"，程序算不出来，所以要留 why 给人核。

命令行::

    api/.venv/bin/python -m pipeline.graph_build merge
    api/.venv/bin/python -m pipeline.graph_build edges
    api/.venv/bin/python -m pipeline.graph_build relate --limit 40
    api/.venv/bin/python -m pipeline.graph_build export --out /tmp/graph.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from sqlalchemy import text

from . import config

sys.path.insert(0, str(config.ROOT / "api"))  # 让 `app.*` 可导入（与 dbstore 同一做法）

VERSION = "v1"

#: 受控关系词表（前端按它配色，别自由加）
RELATION_TYPES = ("requires", "part_of", "contrast_with", "implements", "co_occurs")

#: 归并信号的门槛
TERM_MIN_SHARED = 3
TERM_MIN_JACCARD = 0.5
NAME_MIN_LEN = 6

_WORD_RE = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")


def _engine():
    from app.db import get_engine  # noqa: PLC0415

    return get_engine()


def norm_key(value: str | None) -> str:
    return _WORD_RE.sub("-", str(value or "").lower()).strip("-")


def norm_name(value: str | None) -> str:
    return _WORD_RE.sub("", str(value or "").lower())


# --------------------------------------------------------------------------- 载入


def load_points(conn) -> list[dict]:
    """点 + 它的材料、术语、切片 —— 归并要的三样证据。"""
    rows = [
        dict(row._mapping)
        for row in conn.execute(
            text(
                "SELECT p.id, p.material_id, p.key, p.name, p.kind, p.thickness, p.layers,"
                " p.note, m.slug"
                " FROM knowledge_points p JOIN materials m ON m.id = p.material_id"
                " ORDER BY p.id"
            )
        )
    ]
    terms: dict[tuple[int, str], set[str]] = defaultdict(set)
    for row in conn.execute(text("SELECT material_id, key, terms FROM point_candidates")):
        for term in row[2] or []:
            terms[(row[0], str(row[1]))].add(str(term).strip().lower())
    slices: dict[int, set[str]] = defaultdict(set)
    for row in conn.execute(text("SELECT point_id, slice_id FROM point_sources")):
        if row[1]:
            slices[row[0]].add(str(row[1]))

    for point in rows:
        found = terms.get((point["material_id"], point["key"])) or set()
        if not found:  # 老数据没有候选点：退回 layers 里的词
            found = {str(x).strip().lower() for x in (point["layers"] or []) if str(x).strip()}
        point["terms"] = {t for t in found if len(t) > 1}
        point["slices"] = slices.get(point["id"], set())
    return rows


def mirror_keys(conn) -> tuple[set[str], set[str]]:
    """区分「人写的考纲节点」与「点的镜像叶子」。

    判据是可计算的：key 等于某个知识点/候选点的 key 的，就是镜像 ——
    实测 593 个主题里有 452 个是这么来的（tt-metal 下 443 片就是它们）。
    """
    points = {row[0] for row in conn.execute(text("SELECT DISTINCT key FROM knowledge_points"))}
    candidates = {row[0] for row in conn.execute(text("SELECT DISTINCT key FROM point_candidates"))}
    mirrors = points | candidates
    curated = {row[0] for row in conn.execute(text("SELECT key FROM topics"))} - mirrors
    return curated, mirrors


# --------------------------------------------------------------------------- 归并


def _union_find(size: int):
    parent = list(range(size))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> bool:
        a, b = find(left), find(right)
        if a == b:
            return False
        if a > b:
            a, b = b, a
        parent[b] = a
        return True

    return find, union


def cluster_points(points: list[dict]) -> tuple[dict[int, list[int]], dict[int, list[str]]]:
    """按三条确定性信号归并：同 key → 同名 → 术语高度重合。

    刻意只用**可解释**的信号：每个簇都要能说清"凭什么是同一件事"，
    说不清的宁可不并（留给人看），也不要把两个概念搅在一起。
    """
    find, union = _union_find(len(points))
    reasons: dict[int, list[str]] = defaultdict(list)

    by_key: dict[str, list[int]] = defaultdict(list)
    by_name: dict[str, list[int]] = defaultdict(list)
    by_term: dict[str, list[int]] = defaultdict(list)
    for index, point in enumerate(points):
        by_key[norm_key(point["key"])].append(index)
        name = norm_name(point["name"])
        if len(name) >= NAME_MIN_LEN:
            by_name[name].append(index)
        for term in point["terms"]:
            by_term[term].append(index)

    for key, group in by_key.items():
        for other in group[1:]:
            if union(group[0], other):
                reasons[find(group[0])].append(f"同 key（{key}）")

    for name, group in by_name.items():
        for other in group[1:]:
            if union(group[0], other):
                reasons[find(group[0])].append(f"同名（{points[group[0]]['name']}）")

    # 术语共现计数：只数"有辨识度"的词（出现 ≤ 40 个点的），否则通用词会把一切连起来
    shared: dict[tuple[int, int], int] = defaultdict(int)
    for term, group in by_term.items():
        if len(group) > 40 or len(group) < 2:
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                shared[(group[i], group[j])] += 1
    for (i, j), count in shared.items():
        if count < TERM_MIN_SHARED:
            continue
        union_terms = points[i]["terms"] | points[j]["terms"]
        if len(union_terms) and count / len(union_terms) < TERM_MIN_JACCARD:
            continue
        if union(i, j):
            reasons[find(i)].append(f"术语重合 {count} 个")

    clusters: dict[int, list[int]] = defaultdict(list)
    for index in range(len(points)):
        clusters[find(index)].append(index)
    return clusters, {key: value for key, value in reasons.items()}


def link_questions(conn) -> dict:
    """把题挂到**概念**上 —— 旧题也在这里被适配。

    一条题覆盖的多个点常常属于**同一个概念**（同一概念的多处出现），
    所以按 (题, 概念) 去重、`is_primary` 用 bool_or 合并，而不是照搬点上那条边。

    这一步是"点算证据、概念算统计"的落地点：覆盖率、选题、掌握度、复习计划
    全部应该查 `question_concepts`，而不是 `question_points`（后者按点算，
    同一概念会被算十次，分母虚高十倍）。
    """
    conn.execute(text("DELETE FROM question_concepts"))
    conn.execute(
        text(
            """
            INSERT INTO question_concepts (question_id, concept_id, is_primary)
            SELECT qp.question_id, k.concept_id, bool_or(qp.is_primary)
            FROM question_points qp
            JOIN knowledge_points k ON k.id = qp.point_id
            WHERE k.concept_id IS NOT NULL
            GROUP BY 1, 2
            """
        )
    )
    # 概念上的题量以这张表为准（归并时先按点写的那个值只是中间产物）
    conn.execute(
        text(
            """
            UPDATE concepts c SET question_count = COALESCE(s.n, 0), updated_at = now()
            FROM (SELECT concept_id, COUNT(DISTINCT question_id) n
                  FROM question_concepts GROUP BY 1) s
            WHERE s.concept_id = c.id
            """
        )
    )
    conn.execute(
        text(
            "UPDATE concepts SET question_count = 0, updated_at = now()"
            " WHERE id NOT IN (SELECT DISTINCT concept_id FROM question_concepts)"
        )
    )
    edges = conn.execute(text("SELECT COUNT(*) FROM question_concepts")).scalar()
    covered = conn.execute(
        text("SELECT COUNT(DISTINCT concept_id) FROM question_concepts")
    ).scalar()
    return {"edges": edges, "covered": covered}


def merge_concepts() -> dict:
    """把点归并成概念并落库（幂等：可反复跑）。"""
    with _engine().begin() as conn:
        points = load_points(conn)
        curated, _mirrors = mirror_keys(conn)
        clusters, reasons = cluster_points(points)

        # 概念 → 覆盖它的题（用来定 topic 与题量，也决定"这个考点练过没有"）
        questions = defaultdict(set)
        for row in conn.execute(
            text(
                "SELECT qp.point_id, qp.question_id, q.topic FROM question_points qp"
                " JOIN questions q ON q.id = qp.question_id"
                " WHERE q.retired_at IS NULL"
            )
        ):
            questions[row[0]].add((row[1], row[2] or ""))

        live_keys: set[str] = set()
        created = merged = 0
        for members in clusters.values():
            group = [points[i] for i in members]
            key = Counter(norm_key(p["key"]) for p in group).most_common(1)[0][0]
            if not key:
                continue
            live_keys.add(key)
            names = Counter(p["name"] for p in group if p["name"])
            name = names.most_common(1)[0][0] if names else key
            kind = Counter(p["kind"] for p in group).most_common(1)[0][0]
            aliases = sorted({norm_key(p["key"]) for p in group} - {key})
            # 定义取最长的一条 note —— 点里的 note 是人类可读的那句话
            note = max((p["note"] or "" for p in group), key=len, default="")
            materials = {p["slug"] for p in group}
            covered = set()
            topic_votes: Counter[str] = Counter()
            for p in group:
                for question_id, topic in questions.get(p["id"], set()):
                    covered.add(question_id)
                    if topic:
                        topic_votes[topic] += 1

            # topic 挂靠：概念 key 本身是人写的考纲节点就直接用；否则用镜像叶子的父节点。
            # 这一步把"443 片碎片"收回到"几十个真主题"下面。
            topic_key = key if key in curated else ""
            if not topic_key:
                mirror = conn.execute(
                    text("SELECT parent_key FROM topics WHERE key = :k"), {"k": key}
                ).scalar()
                if mirror and mirror in curated:
                    topic_key = str(mirror)
            if not topic_key and topic_votes:
                for topic, _ in topic_votes.most_common():
                    if topic in curated:
                        topic_key = topic
                        break

            confidence = 1.0 if key in {norm_key(p["key"]) for p in group} else 0.8
            if not aliases and len(group) == 1:
                confidence = 1.0
            elif any("术语" in r for r in reasons.get(members[0], [])) and not aliases:
                confidence = 0.7

            conn.execute(
                text(
                    """
                    INSERT INTO concepts (key, name, kind, definition, topic_key, aliases,
                                          point_count, material_count, question_count,
                                          status, confidence)
                    VALUES (:key, :name, :kind, :definition, :topic, CAST(:aliases AS JSONB),
                            :points, :materials, :questions, 'auto', :confidence)
                    ON CONFLICT (key) DO UPDATE SET
                      name = EXCLUDED.name, kind = EXCLUDED.kind,
                      definition = EXCLUDED.definition, topic_key = EXCLUDED.topic_key,
                      aliases = EXCLUDED.aliases, point_count = EXCLUDED.point_count,
                      material_count = EXCLUDED.material_count,
                      question_count = EXCLUDED.question_count,
                      confidence = EXCLUDED.confidence, updated_at = now()
                    """
                ),
                {
                    "key": key,
                    "name": name,
                    "kind": kind,
                    "definition": note,
                    "topic": topic_key,
                    "aliases": json.dumps(aliases, ensure_ascii=False),
                    "points": len(group),
                    "materials": len(materials),
                    "questions": len(covered),
                    "confidence": confidence,
                },
            )
            concept_id = conn.execute(
                text("SELECT id FROM concepts WHERE key = :k"), {"k": key}
            ).scalar()
            conn.execute(
                text("UPDATE knowledge_points SET concept_id = :cid WHERE id = ANY(:ids)"),
                {"cid": concept_id, "ids": [p["id"] for p in group]},
            )
            created += 1
            merged += len(group) - 1

        # 清掉上一轮自动归并、这一轮不再存在的概念（人确认过的留着）
        if live_keys:
            conn.execute(
                text(
                    "DELETE FROM concepts WHERE status = 'auto' AND key <> ALL(:keys)"
                ),
                {"keys": list(live_keys)},
            )
        linked = link_questions(conn)
        stats = {
            "points": len(points),
            "concepts": created,
            "merged_away": merged,
            "multi_material": sum(
                1
                for members in clusters.values()
                if len({points[i]["slug"] for i in members}) > 1
            ),
            "questions_linked": linked["edges"],
            "concepts_covered": linked["covered"],
        }
    return stats


# --------------------------------------------------------------------------- 派生边


def build_edges() -> dict:
    """程序能确定的边：同切片共现（当证据用，不是知识）。

    这一步刻意保守 —— 它只回答"这两个概念在同一段原文里一起被抽出来过"，
    至于它们到底是前置、组成还是易混，交给 ``relate`` 让人/模型来判。
    """
    with _engine().begin() as conn:
        # 按 (材料, 切片) 把概念聚起来，再两两计数
        by_slice: dict[tuple[int, str], set[int]] = defaultdict(set)
        for row in conn.execute(
            text(
                """
                SELECT DISTINCT ps.slice_id, k.material_id, k.concept_id
                FROM point_sources ps
                JOIN knowledge_points k ON k.id = ps.point_id
                WHERE k.concept_id IS NOT NULL AND ps.slice_id <> ''
                """
            )
        ):
            by_slice[(row[1], str(row[0]))].add(int(row[2]))

        counts: Counter[tuple[int, int]] = Counter()
        for members in by_slice.values():
            ordered = sorted(members)
            for i in range(len(ordered)):
                for j in range(i + 1, len(ordered)):
                    counts[(ordered[i], ordered[j])] += 1

        conn.execute(text("DELETE FROM concept_edges WHERE derived_by = 'code'"))
        written = 0
        for (left, right), count in counts.items():
            if count < 1:
                continue
            conn.execute(
                text(
                    "INSERT INTO concept_edges (from_concept_id, to_concept_id, type, why,"
                    " derived_by, weight) VALUES (:a, :b, 'co_occurs', :why, 'code', :w)"
                    " ON CONFLICT (from_concept_id, to_concept_id, type) DO NOTHING"
                ),
                {
                    "a": left,
                    "b": right,
                    "why": f"在 {count} 个切片里同时出现",
                    "w": float(count),
                },
            )
            written += 1
    return {"co_occurs": written}


# --------------------------------------------------------------------------- 语义关系


async def relate_pairs(limit: int = 40, min_weight: float = 2.0) -> dict:
    """让模型给"反复共现"的概念对判语义关系（requires / part_of / …）。

    为什么只喂**共现过**的对：没有共现证据的对有 O(n²) 个，而且大多是噪声；
    共现至少说明它们在同一段原文里被一起讲过 —— 那种"其实是一件事的两面"的关系，
    正是先修/组成关系的高发区。每条边都带 ``why``，人可以核、可以改。
    """
    from . import config  # noqa: PLC0415
    from .llm import LLM, parse_json  # noqa: PLC0415
    from .worker import load_prompt, render  # noqa: PLC0415

    with _engine().begin() as conn:
        rows = conn.execute(
            text(
                """
                SELECT e.id, e.weight, a.key, a.name, a.definition, b.key, b.name, b.definition
                FROM concept_edges e
                JOIN concepts a ON a.id = e.from_concept_id
                JOIN concepts b ON b.id = e.to_concept_id
                WHERE e.type = 'co_occurs' AND e.weight >= :w
                ORDER BY e.weight DESC, e.id
                LIMIT :limit
                """
            ),
            {"w": min_weight, "limit": limit},
        ).all()

    if not rows:
        return {"checked": 0, "typed": 0}

    template = load_prompt("relate")
    typed = 0

    async def ask(row, llm):
        prompt = render(
            template,
            {
                "A_KEY": row[2],
                "A_NAME": row[3] or row[2],
                "A_DEF": (row[4] or "（无）")[:400],
                "B_KEY": row[5],
                "B_NAME": row[6] or row[5],
                "B_DEF": (row[7] or "（无）")[:400],
                "EVIDENCE": f"在 {int(row[1])} 个切片里同时出现",
            },
        )
        try:
            reply = await llm.chat([{"role": "user", "content": prompt}], max_tokens=600)
            return row, parse_json(reply.text)
        except Exception:  # noqa: BLE001 —— 单条判失败不该毁掉整批
            return row, {}

    # 并发判：LLM 自带并发闸门，逐条 await 会把一百多对拖成十几分钟
    async with LLM(config.load()) as llm:
        results = await asyncio.gather(*(ask(row, llm) for row in rows))

    with _engine().begin() as conn:
        for row, data in results:
            relation = str(data.get("relation") or "none").strip()
            if relation not in RELATION_TYPES or relation == "co_occurs":
                continue
            conn.execute(
                text(
                    "UPDATE concept_edges SET type = :type, why = :why, derived_by = 'llm',"
                    " weight = :w WHERE id = :id"
                ),
                {
                    "type": relation,
                    "why": str(data.get("why") or "")[:500],
                    "w": float(row[1]),
                    "id": row[0],
                },
            )
            typed += 1
    return {"checked": len(rows), "typed": typed}


# --------------------------------------------------------------------------- 导出


def export(
    out: Path,
    min_weight: float = 1.0,
    include: tuple[str, ...] = ("concept", "topic", "material"),
) -> dict:
    """导出给前端画的图。

    拼图逻辑**复用服务端那份**（`app.graph.payload`）：页面上的图与导出的图
    必须一模一样，否则"我看到的"和"构建出来的"迟早对不上。
    """
    from app.graph import payload  # noqa: PLC0415

    data = payload(min_weight=min_weight, include=include)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data["stats"]



def stats() -> dict:
    """图谱体检：不只是"有多少"，还要能看出**算得对不对**。

    这张表要当所有下游算法的基石，所以这些数字必须随时看得见：
    有多少概念还没被题覆盖（该出题）、每个概念几道题（够不够）、
    归并把握度低的有哪些（该人看一眼）、有没有自环与双向语义边（该修的脏数据）。
    """
    with _engine().connect() as conn:
        edges = {
            row[0]: row[1]
            for row in conn.execute(text("SELECT type, COUNT(*) FROM concept_edges GROUP BY 1"))
        }
        per_concept = dict(
            conn.execute(
                text(
                    "SELECT n, COUNT(*) FROM (SELECT concept_id, COUNT(*) n"
                    " FROM question_concepts GROUP BY 1) s GROUP BY n ORDER BY n LIMIT 10"
                )
            ).all()
        )
        return {
            "points": conn.execute(text("SELECT COUNT(*) FROM knowledge_points")).scalar(),
            "points_with_concept": conn.execute(
                text("SELECT COUNT(*) FROM knowledge_points WHERE concept_id IS NOT NULL")
            ).scalar(),
            "concepts": conn.execute(text("SELECT COUNT(*) FROM concepts")).scalar(),
            "concepts_multi_material": conn.execute(
                text("SELECT COUNT(*) FROM concepts WHERE material_count > 1")
            ).scalar(),
            "concepts_with_topic": conn.execute(
                text("SELECT COUNT(*) FROM concepts WHERE topic_key <> ''")
            ).scalar(),
            "questions": conn.execute(
                text("SELECT COUNT(*) FROM questions WHERE retired_at IS NULL")
            ).scalar(),
            "question_concept_edges": conn.execute(
                text("SELECT COUNT(*) FROM question_concepts")
            ).scalar(),
            "concepts_covered": conn.execute(
                text("SELECT COUNT(DISTINCT concept_id) FROM question_concepts")
            ).scalar(),
            "concepts_uncovered": conn.execute(
                text(
                    "SELECT COUNT(*) FROM concepts c WHERE c.status <> 'retired'"
                    " AND NOT EXISTS (SELECT 1 FROM question_concepts qc WHERE qc.concept_id = c.id)"
                )
            ).scalar(),
            "questions_per_concept": per_concept,
            "low_confidence_merges": conn.execute(
                text("SELECT COUNT(*) FROM concepts WHERE confidence < 0.8")
            ).scalar(),
            "dirty_edges": {
                "self_loops": conn.execute(
                    text("SELECT COUNT(*) FROM concept_edges WHERE from_concept_id = to_concept_id")
                ).scalar(),
                "bidirectional_semantic": conn.execute(
                    text(
                        "SELECT COUNT(*) FROM concept_edges a JOIN concept_edges b"
                        " ON a.from_concept_id = b.to_concept_id"
                        " AND a.to_concept_id = b.from_concept_id AND a.type = b.type"
                        " WHERE a.type <> 'co_occurs'"
                    )
                ).scalar(),
            },
            "edges": edges,
        }


# --------------------------------------------------------------------------- 命令行


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.graph_build", description="知识图谱构建")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("merge", help="点归并成概念")
    sub.add_parser("edges", help="派生共现边")
    rel = sub.add_parser("relate", help="模型判语义关系")
    rel.add_argument("--limit", type=int, default=40)
    rel.add_argument("--min-weight", type=float, default=2.0)
    sub.add_parser("stats", help="看一眼图现在什么样")
    exp = sub.add_parser("export", help="导出前端要的 JSON")
    exp.add_argument("--out", required=True)
    exp.add_argument("--min-weight", type=float, default=1.0)

    args = parser.parse_args(argv)
    if args.cmd == "merge":
        result = merge_concepts()
        print(f"归并：{result['points']} 个点 → {result['concepts']} 个概念"
              f"（并掉 {result['merged_away']} 个重复出现 · 跨材料概念 {result['multi_material']} 个）")
    elif args.cmd == "edges":
        print("派生边：", build_edges())
    elif args.cmd == "relate":
        print("语义关系：", asyncio.run(relate_pairs(limit=args.limit, min_weight=args.min_weight)))
    elif args.cmd == "export":
        print("导出：", export(Path(args.out), min_weight=args.min_weight))
    elif args.cmd == "stats":
        for key, value in stats().items():
            print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
