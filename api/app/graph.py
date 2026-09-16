"""知识图谱的**读**接口：把库里的概念、关系、考纲、材料拼成一张图。

为什么读逻辑放在这里而不是 `pipeline/graph_build.py`：写（归并、判关系）是流水线的事，
读是服务端的事 —— 前端页面、命令行导出、静态构建都要拿**同一份**图，
放一处才不会出现"页面上的图"和"导出的图"不一样。

图里有三类节点（前端可分别开关）：

* ``concept``  概念 —— 图谱真正的节点，跨材料唯一
* ``topic``    考纲节点 —— 概念挂在它下面；`mirror=true` 的是"点的镜像叶子"（默认隐藏）
* ``material`` 材料 —— 概念在哪些材料里出现过

边的类型分三档，也按档开关：

* **语义关系**（模型的判读，可被人改）：requires / part_of / contrast_with / implements
* **证据**（程序派生）：co_occurs（同切片共现）
* **结构**：belongs_to（概念→考纲）/ appears_in（概念→材料）/ child_of（考纲树）
"""

from __future__ import annotations

from typing import Any, Iterable

from sqlalchemy import text

from .db import get_engine

#: 语义关系（图上的"知识"，只有这四种是模型判读出来的）
SEMANTIC_TYPES = ("requires", "part_of", "contrast_with", "implements")

DEFAULT_INCLUDE = ("concept", "topic", "material")


def _engine():
    return get_engine()


def mirror_keys(conn) -> tuple[set[str], set[str]]:
    """分出「人写的考纲节点」与「点的镜像叶子」。

    判据是可计算的：key 等于某个知识点/候选点的 key 的，就是镜像叶子 —— 实测
    593 个主题里有 452 个是这么来的（tt-metal 下那 443 片碎片就是它们）。
    图上默认把它们藏起来，考纲那侧才是人话。
    """
    points = {row[0] for row in conn.execute(text("SELECT DISTINCT key FROM knowledge_points"))}
    candidates = {row[0] for row in conn.execute(text("SELECT DISTINCT key FROM point_candidates"))}
    mirrors = points | candidates
    curated = {row[0] for row in conn.execute(text("SELECT key FROM topics"))} - mirrors
    return curated, mirrors


def payload(
    min_weight: float = 1.0,
    include: Iterable[str] = DEFAULT_INCLUDE,
    with_questions: bool = True,
) -> dict[str, Any]:
    """整张图（节点 + 边）。前端一次拿全，不再来回请求。"""
    include = tuple(include)
    nodes: list[dict] = []
    links: list[dict] = []

    with _engine().connect() as conn:
        curated, mirrors = mirror_keys(conn)
        concept_rows = conn.execute(
            text(
                "SELECT id, key, name, kind, definition, topic_key, aliases, point_count,"
                " material_count, question_count, confidence, status FROM concepts"
                " WHERE status <> 'retired' ORDER BY point_count DESC"
            )
        ).all()

        if "concept" in include:
            for row in concept_rows:
                nodes.append(
                    {
                        "id": f"c{row[0]}",
                        "type": "concept",
                        "label": row[2] or row[1],
                        "key": row[1],
                        "kind": row[3],
                        "definition": row[4] or "",
                        "topic": row[5] or "",
                        "aliases": row[6] or [],
                        "points": row[7],
                        "materials": row[8],
                        "questions": row[9],
                        "confidence": row[10],
                        "status": row[11],
                    }
                )
            for row in concept_rows:
                if row[5]:
                    links.append(
                        {
                            "source": f"c{row[0]}",
                            "target": f"t:{row[5]}",
                            "type": "belongs_to",
                            "weight": 2.0,
                        }
                    )
            for row in conn.execute(
                text(
                    "SELECT k.concept_id, m.slug, COUNT(*) FROM knowledge_points k"
                    " JOIN materials m ON m.id = k.material_id"
                    " WHERE k.concept_id IS NOT NULL GROUP BY 1, 2"
                )
            ):
                links.append(
                    {
                        "source": f"c{row[0]}",
                        "target": f"m:{row[1]}",
                        "type": "appears_in",
                        "weight": float(row[2]),
                    }
                )

        if "topic" in include:
            for row in conn.execute(
                text(
                    "SELECT key, name, depth, parent_key, is_leaf, order_index FROM topics"
                    " ORDER BY depth, order_index"
                )
            ):
                nodes.append(
                    {
                        "id": f"t:{row[0]}",
                        "type": "topic",
                        "label": row[1] or row[0],
                        "key": row[0],
                        "depth": row[2],
                        "mirror": row[0] in mirrors and row[0] not in curated,
                        "leaf": row[4],
                    }
                )
                if row[3]:
                    links.append(
                        {
                            "source": f"t:{row[0]}",
                            "target": f"t:{row[3]}",
                            "type": "child_of",
                            "weight": 1.0,
                        }
                    )

        if "material" in include:
            for row in conn.execute(text("SELECT slug, title, subject FROM materials")):
                nodes.append(
                    {
                        "id": f"m:{row[0]}",
                        "type": "material",
                        "label": row[1] or row[0],
                        "key": row[0],
                        "subject": row[2],
                    }
                )

        for row in conn.execute(
            text(
                "SELECT from_concept_id, to_concept_id, type, why, derived_by, weight"
                " FROM concept_edges WHERE weight >= :w ORDER BY weight DESC"
            ),
            {"w": min_weight},
        ):
            links.append(
                {
                    "source": f"c{row[0]}",
                    "target": f"c{row[1]}",
                    "type": row[2],
                    "why": row[3],
                    "by": row[4],
                    "weight": float(row[5]),
                }
            )

        # 概念上挂的题号：点开一个概念就能看到"它在练什么"，也解释了 size 的含义
        if with_questions and "concept" in include:
            by_concept: dict[int, list[str]] = {}
            for row in conn.execute(
                text(
                    "SELECT DISTINCT k.concept_id, q.id FROM knowledge_points k"
                    " JOIN question_points qp ON qp.point_id = k.id"
                    " JOIN questions q ON q.id = qp.question_id"
                    " WHERE k.concept_id IS NOT NULL AND q.retired_at IS NULL"
                    " ORDER BY q.id"
                )
            ):
                by_concept.setdefault(row[0], []).append(row[1])
            wanted = {node["id"]: node for node in nodes if node["type"] == "concept"}
            for concept_id, ids in by_concept.items():
                node = wanted.get(f"c{concept_id}")
                if node is not None:
                    node["questionIds"] = ids[:12]

    ids = {node["id"] for node in nodes}
    links = [link for link in links if link["source"] in ids and link["target"] in ids]
    return _graph_payload(nodes, links)


def _graph_payload(nodes: list[dict], links: list[dict]) -> dict[str, Any]:
    return {
        "version": "v1",
        "stats": {
            "nodes": len(nodes),
            "links": len(links),
            "concepts": sum(1 for n in nodes if n["type"] == "concept"),
            "topics": sum(1 for n in nodes if n["type"] == "topic"),
            "materials": sum(1 for n in nodes if n["type"] == "material"),
            "semantic": sum(1 for link in links if link["type"] in SEMANTIC_TYPES),
        },
        "nodes": nodes,
        "links": links,
    }


def diagnose(question_ids: list[str], depth: int = 2) -> dict[str, Any]:
    """把"这几道题错了"翻译成"该去补哪个考点"。

    这是图谱对用户可见的第一件事：复习页不再只说"这题错了"，而是沿着
    `requires`（前置）边往回走，指出**根因考点**与**建议的顺序**。

    只回到考点与先后关系这一步，不下"你掌握了没有"的结论 ——
    掌握度是个人的、在客户端；这里给的是"这条链长什么样"，
    前端拿它和自己的掌握度一对，就能说"先补 A，再回来做 B"。
    """
    if not question_ids:
        return {"wrongConcepts": [], "prerequisites": [], "order": []}

    with _engine().connect() as conn:
        info = {
            row[0]: {"key": row[1], "name": row[2], "topic": row[3] or "", "questions": row[4]}
            for row in conn.execute(
                text(
                    "SELECT id, key, name, topic_key, question_count FROM concepts"
                    " WHERE status <> 'retired'"
                )
            )
        }
        wrong = [
            row[0]
            for row in conn.execute(
                text(
                    "SELECT DISTINCT concept_id FROM question_concepts"
                    " WHERE question_id = ANY(:ids)"
                ),
                {"ids": list(question_ids)},
            )
        ]
        prereq: dict[int, list[tuple[int, str, str]]] = {}
        for row in conn.execute(
            text(
                "SELECT from_concept_id, to_concept_id, why, derived_by FROM concept_edges"
                " WHERE type = 'requires'"
            )
        ):
            prereq.setdefault(row[1], []).append((row[0], row[2] or "", row[3] or ""))

        # 广度回溯：谁挡在它前面（含更前面一层），去重后按"离错题的距离"排序
        seen: dict[int, int] = {}
        frontier = [(cid, 0) for cid in wrong]
        while frontier:
            cid, distance = frontier.pop(0)
            if distance >= depth:
                continue
            for source, why, by in prereq.get(cid, []):
                if source in seen or source in wrong:
                    continue
                seen[source] = distance + 1
                frontier.append((source, distance + 1))

        def brief(cid: int) -> dict:
            data = info.get(cid, {})
            return {
                "key": data.get("key", ""),
                "name": data.get("name", ""),
                "topic": data.get("topic", ""),
                "questions": data.get("questions", 0),
            }

    return {
        "wrongConcepts": [brief(cid) for cid in wrong],
        "prerequisites": [
            {**brief(cid), "hops": hops} for cid, hops in sorted(seen.items(), key=lambda x: x[1])
        ],
        # 先补更远的前置，再回到错题本身 —— 这就是"先练哪几道"的顺序
        "order": [brief(cid) for cid, _ in sorted(seen.items(), key=lambda x: -x[1])]
        + [brief(cid) for cid in wrong],
    }
