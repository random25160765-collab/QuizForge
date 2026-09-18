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

from sqlalchemy import bindparam, text

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


def diagnose(question_ids: list[str], depth: int = 8) -> dict[str, Any]:
    """把"这几道题错了"翻译成"该去补哪个考点、按什么顺序、几步"。

    这是图谱对用户可见的第一件事：复习页不再只说"这题错了"，而是沿着
    `requires`（前置）边往回走，指出**根因考点**、**整条前置链**与**建议顺序**。

    为什么只走 `requires` 不走 `part_of`：`part_of` 说的是"谁是谁的一部分"，
    是结构不是先后 —— "先学 A 再学 B"这件事只有 `requires` 说了算。

    为什么不下"你掌握了没有"的判语：掌握度是个人的、在客户端。
    这里只给"这条链长什么样"（含每一步的距离与依据），前端拿它和自己的掌握度一对，
    就能说"先补 A，再回来做 B"。

    返回（`wrongConcepts` / `prerequisites` / `order` 三个键是老的，保持不动）：

    * `chains` —— 每道错题一条链：从最远的前置一路排到这道错题本身；
    * `steps` —— 最长那条链的步数（含错题本身）；
    * `roots` —— 链的起点（它自己没有更前置的东西，"从这里开始"就是它）；
    * `truncated` —— 撞到了 `depth` 上限（链还没走完，别把它当成"没有前置"）。
    """
    if not question_ids:
        return {
            "wrongConcepts": [],
            "prerequisites": [],
            "order": [],
            "chains": [],
            "steps": 0,
            "roots": [],
            "truncated": False,
        }


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
                # `IN :ids` + expanding：SQLite 没有 `ANY`（那是 Postgres 的）
                text(
                    "SELECT DISTINCT concept_id FROM question_concepts"
                    " WHERE question_id IN :ids"
                ).bindparams(bindparam("ids", expanding=True)),
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

        def brief(cid: int) -> dict:
            data = info.get(cid, {})
            return {
                "key": data.get("key", ""),
                "name": data.get("name", ""),
                "topic": data.get("topic", ""),
                "questions": data.get("questions", 0),
            }

        def is_root(cid: int) -> bool:
            """它自己还有没有"得先学"的东西 —— 没有就是链的起点。"""
            return not [src for src, _why, _by in prereq.get(cid, []) if src not in wrong]

        flags = {"truncated": False}

        def walk(origin: int) -> tuple[dict[int, int], dict[int, tuple[str, str]]]:
            """从一道错题往回走：{前置: 几步} 与 {前置: (依据, 谁判的)}。

            广度优先，所以每个前置拿到的是**最短**那条路（"几步能补到"）。
            撞到 `depth` 就停并标记 —— 停下来的那个不是"没有前置"。
            """
            local: dict[int, int] = {}
            reason: dict[int, tuple[str, str]] = {}
            frontier = [(origin, 0)]
            while frontier:
                cid, distance = frontier.pop(0)
                for source, why, by in prereq.get(cid, []):
                    if source in wrong:
                        continue
                    step = distance + 1
                    if step > depth:
                        flags["truncated"] = True
                        continue
                    if source in local and local[source] <= step:
                        continue
                    local[source] = step
                    reason.setdefault(source, (why, by))
                    frontier.append((source, step))
            return local, reason

        chains: list[dict] = []
        hops: dict[int, int] = {}
        via: dict[int, tuple[str, str]] = {}
        for origin in wrong:
            local, reason = walk(origin)
            for cid, step in local.items():
                if cid not in hops or step < hops[cid]:
                    hops[cid] = step
                via.setdefault(cid, reason.get(cid, ("", "")))
            # 链是"从最远的前置往下排到错题本身" —— 那就是该照着补的顺序
            chains.append(
                {
                    "wrong": brief(origin),
                    "steps": [
                        {
                            **brief(cid),
                            "hops": step,
                            "isRoot": is_root(cid),
                            "why": reason.get(cid, ("", ""))[0],
                            "derivedBy": reason.get(cid, ("", ""))[1],
                        }
                        for cid, step in sorted(local.items(), key=lambda kv: (-kv[1], kv[0]))
                    ]
                    + [brief(origin)],
                }
            )

    # 越远的越先补："几步"是从错题往回数的，所以降序就是补课顺序
    ranked = sorted(hops.items(), key=lambda kv: (-kv[1], info.get(kv[0], {}).get("key", "")))

    def detail(cid: int, step: int) -> dict:
        return {
            **brief(cid),
            "hops": step,
            "isRoot": is_root(cid),
            "why": via.get(cid, ("", ""))[0],
            "derivedBy": via.get(cid, ("", ""))[1],
        }

    return {
        "wrongConcepts": [brief(cid) for cid in wrong],
        "prerequisites": [detail(cid, step) for cid, step in ranked],
        # 先补更远的前置，再回到错题本身 —— 这就是"先练哪几道"的顺序
        "order": [brief(cid) for cid, _ in ranked] + [brief(cid) for cid in wrong],
        "chains": chains,
        "steps": max((len(chain["steps"]) for chain in chains), default=0),
        "roots": [brief(cid) for cid, _step in ranked if is_root(cid)],
        "truncated": flags["truncated"],
    }
