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
* ``check`` —— 体检：图能不能支撑"下一步该学什么"（无环、能走到根、闭包不矛盾）。
  它是补边的进度尺，也是唯一的闸门 —— 破了就退出码非零。

命令行::

    api/.venv/bin/python -m pipeline.graph_build merge
    api/.venv/bin/python -m pipeline.graph_build edges
    api/.venv/bin/python -m pipeline.graph_build relate --limit 40
    api/.venv/bin/python -m pipeline.graph_build check
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

#: **有序**关系：`from` 是主动那头 —— `A →requires→ B` 读作「A 是 B 的前置」，
#: `A →part_of→ B` 读作「A 是 B 的一部分」。只有这两类参与前置链与体检 ——
#: `contrast_with`（易混）是**对称**的，`co_occurs`（共现）只是证据，都不排先后。
ORDERED_TYPES = ("requires", "part_of")

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

    判据是可计算的：key 等于某个知识点/候选点的 key、**并且它不是任何节点的父节点**
    —— 就是镜像（实测 593 个主题里 452 个是这么来的，tt-metal 下 443 片就是它们）。

    为什么必须加"并且不是父节点"这半句：`tt-arch` 既是人写的学科根（Tenstorrent），
    又恰好有一个同名知识点 —— 只看前半句就会把**学科根**误判成镜像，
    于是挂在它下面的概念全都推不出考纲（实测 14 个概念因此到不了根，2026-09-18 抓到的）。
    人写的节点有下级，镜像叶子没有。
    """
    points = {row[0] for row in conn.execute(text("SELECT DISTINCT key FROM knowledge_points"))}
    candidates = {row[0] for row in conn.execute(text("SELECT DISTINCT key FROM point_candidates"))}
    parents = {
        row[0]
        for row in conn.execute(
            text("SELECT DISTINCT parent_key FROM topics WHERE COALESCE(parent_key, '') <> ''")
        )
    }
    mirrors = (points | candidates) - parents
    curated = {row[0] for row in conn.execute(text("SELECT key FROM topics"))} - mirrors
    return curated, mirrors


def nearest_curated(key: str, curated: set[str], parent_of: dict[str, str]) -> str:
    """从一个 topic key 往上找**最近的人写考纲节点**（找不到返回空串）。

    为什么不能只看一层父节点：镜像叶子也可能挂在另一片镜像下面。实测有 21 个概念
    挂不上考纲（于是它们在图上到不了根）—— 根因是"覆盖它的题的 topic 是镜像叶子，
    而镜像不在人写清单里"。往上走到最近的人写节点，才是"443 片碎片收回几十个真主题"
    这件事该有的完整形状。
    """
    seen: set[str] = set()
    node = key
    while node and node not in seen:
        if node in curated:
            return node
        seen.add(node)
        node = parent_of.get(node, "")
    return ""


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
            -- `MAX` 而不是 Postgres 的 `bool_or`：SQLite 里布尔就是 0/1，
            -- 取最大值与"只要有一处是主要就算主要"同义（换引擎时把方言函数换掉）
            SELECT qp.question_id, k.concept_id, MAX(qp.is_primary)
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
            UPDATE concepts c SET question_count = COALESCE(s.n, 0), updated_at = CURRENT_TIMESTAMP
            FROM (SELECT concept_id, COUNT(DISTINCT question_id) n
                  FROM question_concepts GROUP BY 1) s
            WHERE s.concept_id = c.id
            """
        )
    )
    conn.execute(
        text(
            "UPDATE concepts SET question_count = 0, updated_at = CURRENT_TIMESTAMP"
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
        # 考纲树整个读进来（454 个节点）：挂靠要**往上走**，一层一层查库太笨
        parent_of = {
            row[0]: (row[1] or "")
            for row in conn.execute(text("SELECT key, parent_key FROM topics")).all()
        }
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
        # 材料 → 该材料里概念的考纲票（第二遍兜底用：见下面"借同材料的多数票"）
        material_votes: dict[str, Counter[str]] = defaultdict(Counter)
        # 这一遍**没挂上考纲**的：（概念 id，它来自哪些材料）
        pending_topic: list[tuple[int, set[str]]] = []
        filled_topic = 0
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

            # topic 挂靠：概念 key 本身是人写的考纲节点就直接用；否则**往上走到最近的
            # 人写节点**（概念自己可能是镜像，覆盖它的题的 topic 也可能还是镜像）。
            # 这一步把"443 片碎片"收回到"几十个真主题"下面 —— 走到最近的人写节点才算走完。
            topic_key = key if key in curated else ""
            if not topic_key:
                topic_key = nearest_curated(parent_of.get(key, ""), curated, parent_of)
            if not topic_key and topic_votes:
                for topic, _ in topic_votes.most_common():
                    topic_key = nearest_curated(topic, curated, parent_of)
                    if topic_key:
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
                    VALUES (:key, :name, :kind, :definition, :topic, :aliases,
                            :points, :materials, :questions, 'auto', :confidence)
                    ON CONFLICT (key) DO UPDATE SET
                      name = EXCLUDED.name, kind = EXCLUDED.kind,
                      definition = EXCLUDED.definition,
                      -- 归并**不该把已经挂好的考纲抹掉**：这次算出来是空、库里已有值，就留着。
                      -- 2026-09-18 实测踩过：直接 `topic_key = EXCLUDED.topic_key` 一把
                      -- 冲掉了 456 个概念的挂靠（它们在图上当场变成到不了根）。
                      topic_key = CASE WHEN EXCLUDED.topic_key <> '' THEN EXCLUDED.topic_key
                                       ELSE concepts.topic_key END,
                      aliases = EXCLUDED.aliases, point_count = EXCLUDED.point_count,
                      material_count = EXCLUDED.material_count,
                      question_count = EXCLUDED.question_count,
                      confidence = EXCLUDED.confidence, updated_at = CURRENT_TIMESTAMP
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
            if topic_key:
                for slug in materials:
                    material_votes[slug][topic_key] += 1
            else:
                pending_topic.append((int(concept_id), materials))
            created += 1
            merged += len(group) - 1

        # 第二遍：还没挂上的，**借同一个材料里其它概念的多数票**。
        # 依据很直白：一份材料通常属于一个考纲单元，同材料的点大多挂在同一个节点下面。
        # 兜底而已 —— 首选项仍是"自己就是考纲 / 镜像往上走 / 覆盖它的题怎么说"。
        # 实测（2026-09-18）：这一步把最后 6 个（既没题、也没镜像父节点）挂上了，
        # 概念的挂靠从 906/912 补齐到 912/912。
        for concept_id, slugs in pending_topic:
            votes: Counter[str] = Counter()
            for slug in slugs:
                votes.update(material_votes.get(slug) or {})
            if votes:
                conn.execute(
                    text("UPDATE concepts SET topic_key = :t WHERE id = :id"),
                    {"t": votes.most_common(1)[0][0], "id": concept_id},
                )
                filled_topic += 1

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
            "topic_filled_by_material": filled_topic,
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
        # 已经有语义关系的配对不再补共现边：共现边是"待判"的候选，判过了就该退出池子。
        # 不排掉的话每次 `edges` 都把判过的配对捞回来，`relate` 下次再问一遍（白花钱）——
        # 2026-09-18 实测：一次 `make graph` 复活了 1152 对。
        known = {
            (row[0], row[1])
            for row in conn.execute(
                text("SELECT from_concept_id, to_concept_id FROM concept_edges WHERE type <> 'co_occurs'")
            )
        }
        written = 0
        for (left, right), count in counts.items():
            if count < 1 or (left, right) in known:
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


#: 判过"没有关系"的边打这个标记。
#: 为什么要留痕：模型说 `none` 的边**不会改 type**，于是下次查询照样选中它 ——
#: 重跑一遍就把同一批再问一遍，钱白花。判失败的（异常）**不打**标记，
#: 那种本来就该重试（见 `relate_pairs` 的回写循环）。
JUDGED_NONE = "llm_none"

#: 两端说明都短于这个长度的对**不问模型**：说明太短多半是抽取时留下的噪声点
#: （实测 83 个概念的说明不足 20 字），问它等于花钱买一个 `none`。
MIN_DEFINITION_CHARS = 20


def _relate_candidates(limit: int, min_weight: float) -> list[dict]:
    """挑出这一批要判的共现对（顺序就是这个顺序，可复现）。

    为什么排序不是单纯按 weight：补边的目标是**每个概念都能走到根**，
    所以优先问「还没接进有序边」的概念 —— 一个孤点的第一条前置边才是把路径接起来的那条，
    而两端都已经接进图里的对，判出来只是锦上添花。同档之内才按 weight 降序。
    """
    with _engine().connect() as conn:
        rows = conn.execute(
            text(
                """
                WITH ordered_degree AS (
                    SELECT c.id AS id, COUNT(e.id) AS n
                    FROM concepts c
                    LEFT JOIN concept_edges e
                      ON e.type IN ('requires', 'part_of')
                     AND (e.from_concept_id = c.id OR e.to_concept_id = c.id)
                    GROUP BY c.id
                )
                SELECT e.id AS edge_id, e.weight AS weight,
                       a.key AS a_key, a.name AS a_name, a.definition AS a_def,
                       b.key AS b_key, b.name AS b_name, b.definition AS b_def
                FROM concept_edges e
                JOIN concepts a ON a.id = e.from_concept_id
                JOIN concepts b ON b.id = e.to_concept_id
                LEFT JOIN ordered_degree da ON da.id = a.id
                LEFT JOIN ordered_degree db ON db.id = b.id
                WHERE e.type = 'co_occurs'
                  AND e.derived_by <> :judged
                  -- 这一对已经有语义关系了：答案已经买到，不再问
                  AND NOT EXISTS (
                      SELECT 1 FROM concept_edges x
                      WHERE x.from_concept_id = e.from_concept_id
                        AND x.to_concept_id = e.to_concept_id
                        AND x.type <> 'co_occurs'
                  )
                  AND e.weight >= :w
                  AND length(COALESCE(a.definition, '')) >= :min_def
                  AND length(COALESCE(b.definition, '')) >= :min_def
                ORDER BY (COALESCE(da.n, 0) + COALESCE(db.n, 0)), e.weight DESC, e.id
                LIMIT :limit
                """
            ),
            {
                "w": min_weight,
                "limit": limit,
                "judged": JUDGED_NONE,
                "min_def": MIN_DEFINITION_CHARS,
            },
        ).all()
    return [dict(row._mapping) for row in rows]


async def relate_pairs(limit: int = 40, min_weight: float = 2.0) -> dict:
    """让模型给"反复共现"的概念对判语义关系（requires / part_of / …）。

    为什么只喂**共现过**的对：没有共现证据的对有 O(n²) 个，而且大多是噪声；
    共现至少说明它们在同一段原文里被一起讲过 —— 那种"其实是一件事的两面"的关系，
    正是先修/组成关系的高发区。每条边都带 ``why``，人可以核、可以改。

    **幂等可续跑**：判过"没有关系"的边会被标记（`JUDGED_NONE`），下次不再问；
    判失败的不打标记，下次会重问。所以分批跑（`--limit`）是安全的，
    中断了接着跑就行。
    """
    from . import config  # noqa: PLC0415
    from .llm import LLM, parse_json  # noqa: PLC0415
    from .worker import load_prompt, render  # noqa: PLC0415

    rows = _relate_candidates(limit, min_weight)
    if not rows:
        return {"checked": 0, "typed": 0, "none": 0, "failed": 0}

    template = load_prompt("relate")
    typed = 0
    none = 0
    failed = 0

    async def ask(row, llm):
        prompt = render(
            template,
            {
                "A_KEY": row["a_key"],
                "A_NAME": row["a_name"] or row["a_key"],
                "A_DEF": (row["a_def"] or "（无）")[:400],
                "B_KEY": row["b_key"],
                "B_NAME": row["b_name"] or row["b_key"],
                "B_DEF": (row["b_def"] or "（无）")[:400],
                "EVIDENCE": f"在 {int(row['weight'])} 个切片里同时出现",
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
            # 这一条**判失败**（异常/解析不出）：什么都不写 —— 失败不该被记住，
            # 下次重跑会再问一遍，这才是对的。
            if not data:
                failed += 1
                continue

            relation = str(data.get("relation") or "none").strip()
            why = str(data.get("why") or "")[:500]
            if relation in RELATION_TYPES and relation != "co_occurs":
                conn.execute(
                    text(
                        "UPDATE concept_edges SET type = :type, why = :why, derived_by = 'llm',"
                        " weight = :w WHERE id = :id"
                    ),
                    {"type": relation, "why": why, "w": float(row["weight"]), "id": row["edge_id"]},
                )
                typed += 1
                continue

            # 判成 `none`（或模型给了个词表外的词）：**留痕**，边仍然是 co_occurs。
            # 不留痕的话下次查询照样选中它 —— 同一批会被反复问，钱白花。
            conn.execute(
                text(
                    "UPDATE concept_edges SET derived_by = :judged, why = :why WHERE id = :id"
                ),
                {"judged": JUDGED_NONE, "why": why or "判过：两者无明显关系", "id": row["edge_id"]},
            )
            none += 1
    return {"checked": len(rows), "typed": typed, "none": none, "failed": failed}


# --------------------------------------------------------- 判边（以概念为中心）

#: 一次给模型看多少个候选。
#: 12 起手，实测还有一批"候选里确实没有前置"的概念 —— 放宽到 20 再问一次（只问那些
#: 还没接进图、且上一次判空的），给它们一个更宽的挑选面。
CENTRIC_PEER_LIMIT = 20


def _centric_candidates(limit: int) -> list[dict]:
    """挑出这一批要问的**目标概念**：还没接进有序边的那些，各带一组候选。

    与 `_relate_candidates` 的差别是问题的形状：那边问"这两个之间是什么关系"，
    这边问"要读懂它，得先懂哪几个"。后者更接近人排课时的思路，而且一次就能把一个
    孤点接进图 —— 这正是"每个概念都能走到根"缺的那一步。

    ## 候选从哪来（这里踩过一次）

    一开始只拿**同一段切片**里共现过的概念当候选，实测几乎全是"没有前置"：
    两个概念在同一段里被抽出来，多半只是同一页上一起出现过，而**前置常常跨小节**。
    所以候选改成两层，合起来用：

    1. 同切片共现过的（有证据，排前面）；
    2. **同一份材料里**的其它概念（前置关系常常跨小节，这一层把池子放宽）。

    两层都空就跳过 —— 没东西可挑的问题没有意义，也别把它标记成"问过"。
    """
    with _engine().connect() as conn:
        targets = conn.execute(
            text(
                """
                SELECT id, key, name, COALESCE(definition, '') AS definition,
                       COALESCE(topic_key, '') AS topic_key
                FROM concepts c
                WHERE c.centric_at IS NULL
                  AND LENGTH(COALESCE(c.definition, '')) >= :min_def
                  AND NOT EXISTS (
                      SELECT 1 FROM concept_edges e
                      WHERE e.type IN ('requires', 'part_of')
                        AND (e.from_concept_id = c.id OR e.to_concept_id = c.id)
                  )
                ORDER BY c.id
                LIMIT :limit
                """
            ),
            {"min_def": MIN_DEFINITION_CHARS, "limit": limit},
        ).all()

        out: list[dict] = []
        for row in targets:
            concept_id = int(row[0])
            # ① 同切片共现过的
            peers = [
                {
                    "id": int(peer[0]),
                    "key": peer[1],
                    "name": peer[2],
                    "definition": peer[3],
                }
                for peer in conn.execute(
                    text(
                        """
                        SELECT DISTINCT p.id, p.key, p.name, COALESCE(p.definition, '') AS definition
                        FROM concept_edges e
                        JOIN concepts p ON p.id = CASE
                            WHEN e.from_concept_id = :cid THEN e.to_concept_id
                            ELSE e.from_concept_id
                        END
                        WHERE e.type = 'co_occurs'
                          AND (e.from_concept_id = :cid OR e.to_concept_id = :cid)
                        ORDER BY p.id
                        """
                    ),
                    {"cid": concept_id},
                ).all()
            ]
            # ② 同一份材料里的其它概念（共现过的排前面）
            seen = {peer["id"] for peer in peers}
            for peer in conn.execute(
                text(
                    """
                    SELECT p.id, p.key, p.name, COALESCE(p.definition, '') AS definition
                    FROM knowledge_points self
                    JOIN knowledge_points other ON other.material_id = self.material_id
                    JOIN concepts p ON p.id = other.concept_id
                    WHERE self.concept_id = :cid AND p.id <> :cid
                    GROUP BY p.id, p.key, p.name, p.definition
                    ORDER BY p.id
                    """
                ),
                {"cid": concept_id},
            ).all():
                if int(peer[0]) in seen:
                    continue
                seen.add(int(peer[0]))
                peers.append(
                    {
                        "id": int(peer[0]),
                        "key": peer[1],
                        "name": peer[2],
                        "definition": peer[3],
                    }
                )
                if len(peers) >= CENTRIC_PEER_LIMIT:
                    break

            peers = peers[:CENTRIC_PEER_LIMIT]
            if not peers:
                continue
            out.append(
                {
                    "id": concept_id,
                    "key": row[1],
                    "name": row[2],
                    "definition": row[3],
                    "topic": row[4],
                    "peers": peers,
                }
            )
        return out


async def relate_centric(limit: int = 20) -> dict:
    """以概念为中心的判边：给模型**一个概念 + 它的候选**，问"要懂它得先懂哪几个"。

    ## 为什么要再开一条路

    配对判边（`relate_pairs`）已经把现网共现对判完了，产出很低（1912 对 → 19 条边）：
    "同切片共现"这个信号本身撑不起"前置"—— 两个概念在同一段里被抽出来，
    多半只是同一页上一起出现过。但"要读懂这个概念得先懂什么"是另一个问题，
    可以带着候选一起问，也可以直接盯着**还没接进图的概念**问。

    ## 三条稳妥的规矩

    * 只加**不产生环**的边（加之前在内存里查一次可达性）—— 图的硬不变量不能破；
    * 候选和它已经有任何边就跳过（不覆盖既有结论）；
    * 问过就记 `centric_at`（模型说"没有前置"的也记）—— 同一批不重复买。
    """
    from . import config  # noqa: PLC0415
    from .llm import LLM, parse_json  # noqa: PLC0415
    from .worker import load_prompt, render  # noqa: PLC0415

    targets = _centric_candidates(limit)
    if not targets:
        return {"checked": 0, "edges": 0, "empty": 0, "cycled": 0, "failed": 0}

    with _engine().connect() as conn:
        ordered = [
            (int(row[0]), int(row[1]))
            for row in conn.execute(
                text(
                    "SELECT from_concept_id, to_concept_id FROM concept_edges"
                    " WHERE type IN ('requires', 'part_of')"
                )
            )
        ]
        known = {
            (int(row[0]), int(row[1]))
            for row in conn.execute(
                text("SELECT from_concept_id, to_concept_id FROM concept_edges")
            )
        }

    adj: dict[int, set[int]] = defaultdict(set)
    for source, target in ordered:
        adj[source].add(target)

    def reaches(start: int, goal: int) -> bool:
        """`start` 顺着有序边能不能走到 `goal`（用来判"加这条边会不会成环"）。"""
        stack = [start]
        seen: set[int] = set()
        while stack:
            node = stack.pop()
            if node == goal:
                return True
            if node in seen:
                continue
            seen.add(node)
            stack.extend(adj.get(node, ()))
        return False

    template = load_prompt("relate_centric")
    edges_added = 0
    empty = 0
    cycled = 0
    failed = 0

    async def ask(target: dict, llm) -> tuple[dict, dict]:  # noqa: ANN001
        peers = "\n".join(
            "- %s · %s · %s" % (peer["key"], peer["name"], peer["definition"][:160])
            for peer in target["peers"]
        )
        prompt = render(
            template,
            {
                "TARGET_KEY": target["key"],
                "TARGET_NAME": target["name"] or target["key"],
                "TARGET_DEF": target["definition"][:400],
                "TARGET_TOPIC": target["topic"] or "（未挂考纲）",
                "PEERS": peers,
            },
        )
        try:
            reply = await llm.chat([{"role": "user", "content": prompt}], max_tokens=800)
            return target, parse_json(reply.text)
        except Exception:  # noqa: BLE001 —— 单条判失败不该毁掉整批
            return target, {}

    async with LLM(config.load()) as llm:
        results = await asyncio.gather(*(ask(target, llm) for target in targets))

    # (字段, 边类型, 是否反过来)：`contains` 是"目标是候选的一部分"，
    # 所以边要反着写（候选 ←part_of← 目标 读作"目标属于候选"）
    fields = (("requires", "requires", False), ("partOf", "part_of", False),
              ("contains", "part_of", True))

    with _engine().begin() as conn:
        for target, data in results:
            target_id = target["id"]
            if not data:
                failed += 1
                continue

            peer_ids = {peer["key"]: peer["id"] for peer in target["peers"]}
            added = 0
            for field, kind, flip in fields:
                items = data.get(field)
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    peer_id = peer_ids.get(str(item.get("key") or "").strip())
                    if peer_id is None:
                        continue
                    source, dest = (target_id, peer_id) if flip else (peer_id, target_id)
                    if source == dest or (source, dest) in known:
                        continue
                    if reaches(dest, source):  # 加进去就成环 —— 丢掉这条边，图不能破
                        cycled += 1
                        continue
                    conn.execute(
                        text(
                            "INSERT INTO concept_edges (from_concept_id, to_concept_id, type,"
                            " why, derived_by, weight) VALUES (:a, :b, :t, :w, 'llm', 1.0)"
                            " ON CONFLICT (from_concept_id, to_concept_id, type) DO NOTHING"
                        ),
                        {
                            "a": source,
                            "b": dest,
                            "t": kind,
                            "w": str(item.get("why") or "")[:500],
                        },
                    )
                    adj[source].add(dest)
                    known.add((source, dest))
                    added += 1

            if not added:
                empty += 1
            edges_added += added
            conn.execute(
                text("UPDATE concepts SET centric_at = CURRENT_TIMESTAMP WHERE id = :id"),
                {"id": target_id},
            )

    return {
        "checked": len(targets),
        "edges": edges_added,
        "empty": empty,
        "cycled": cycled,
        "failed": failed,
    }


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



# --------------------------------------------------------------------------- 体检


def _find_cycles(edges: list[tuple[str, str]]) -> list[list[str]]:
    """找有向环（迭代式三色 DFS），返回若干条环的节点序列。

    为什么不直接在库里用递归 CTE 算：环要**报出路径**人才修得动，而 SQL 里攒路径
    得靠数组再自己复原。这张图只有几百条边，拉到内存里走一遍更快也更清楚。
    """
    out_edges: dict[str, list[str]] = defaultdict(list)
    for src, dst in edges:
        out_edges[src].append(dst)

    WHITE, GREY, BLACK = 0, 1, 2
    color: dict[str, int] = defaultdict(int)
    found: list[list[str]] = []

    for start in list(out_edges):
        if color[start] != WHITE:
            continue
        stack: list[tuple[str, int]] = [(start, 0)]
        path: list[str] = []
        while stack:
            node, index = stack[-1]
            if index == 0:
                color[node] = GREY
                path.append(node)
            neighbours = out_edges.get(node, [])
            if index < len(neighbours):
                stack[-1] = (node, index + 1)
                nxt = neighbours[index]
                if color[nxt] == GREY:
                    found.append(path[path.index(nxt) :] + [nxt])
                elif color[nxt] == WHITE:
                    stack.append((nxt, 0))
            else:
                color[node] = BLACK
                path.pop()
                stack.pop()
    return found


def _longest_chain(prereqs: dict[str, set[str]]) -> int:
    """最长前置链：从任一概念往上走（找它的前置）最多几步。

    有环时按"环里不重复走"兜底 —— 环本身已经被报成硬问题了，这里只求不爆栈。
    """
    memo: dict[str, int] = {}

    def walk(node: str, seen: frozenset[str]) -> int:
        if node in memo:
            return memo[node]
        if node in seen:
            return 0
        deeper = seen | {node}
        best = 0
        for parent in prereqs.get(node, ()):  # type: ignore[union-attr]
            best = max(best, 1 + walk(parent, deeper))
        memo[node] = best
        return best

    return max((walk(node, frozenset()) for node in list(prereqs)), default=0)


def check(limit: int = 5) -> dict:
    """图谱体检：把「图上任取概念能走到根、无环、可传递闭包不矛盾」变成可执行断言。

    ## 三条**硬**不变量（破了就是脏数据，`check` 的退出码非零）

    * `self_loops` —— 自己指向自己；
    * `cycles` —— 有序边上有环。它等价于「沿前置链往上走**永远到不了根**」，
      是三条里最要命的一条：环上的概念排不出一个先后顺序；
    * `kind_conflicts` —— 同一对概念同时被说成「前置」与「组成」（A 是 B 的前提，
      同时又是 B 的一部分），或同一种有序关系**双向**成立（互为前提 / 互为组成部分）。
      **刻意不算 `contrast_with` 的双向**：易混本来就是对称的，两边都写是对的；
      也不算 `co_occurs`（它只是"一起出现过"的证据，不是结论）。

    ## 两条**软**指标（只报出来，不判死）

    * `roots` —— 没有前置的概念：对它们，"该先学什么"的答案就是"它自己"；
    * `unrooted` —— **一条有序边都没有**的概念（这次实测 370 个）。它们不是错，
      但任何路径推荐走到这里只会说"没有前置"，而这句话**没法验证**。补边的进度就看它。
    """
    with _engine().connect() as conn:
        names = {
            row[0]: (row[1] or "", row[2] or "")
            for row in conn.execute(text("SELECT id, key, name FROM concepts")).all()
        }
        rows = conn.execute(
            text("SELECT from_concept_id, to_concept_id, type FROM concept_edges")
        ).all()
        topics = {
            row[0]: (row[1] or "")
            for row in conn.execute(text("SELECT key, parent_key FROM topics")).all()
        }
        concept_topics = {
            row[0]: (row[1] or "")
            for row in conn.execute(text("SELECT id, topic_key FROM concepts")).all()
        }

    def label(concept_id: str) -> str:
        key, name = names.get(concept_id, ("", ""))
        if key and name:
            return f"{key}（{name}）"
        return key or name or concept_id

    ordered = [(row[0], row[1], row[2]) for row in rows if row[2] in ORDERED_TYPES]
    ordered_pairs = set(ordered)

    self_loops = sorted({row[0] for row in rows if row[0] == row[1]})
    cycles = _find_cycles([(src, dst) for src, dst, _kind in ordered])
    bidirectional = sorted(
        {(src, dst, kind) for src, dst, kind in ordered_pairs if (dst, src, kind) in ordered_pairs}
    )
    by_pair: dict[frozenset[str], set[str]] = defaultdict(set)
    for src, dst, kind in ordered_pairs:
        by_pair[frozenset((src, dst))].add(kind)
    kind_conflicts = sorted(pair for pair, kinds in by_pair.items() if len(kinds) > 1)

    prereqs: dict[str, set[str]] = defaultdict(set)
    touched: set[str] = set()
    for src, dst, kind in ordered:
        touched.update((src, dst))
        if kind == "requires":
            prereqs[dst].add(src)

    roots = [cid for cid in names if not prereqs.get(cid)]
    unrooted = [cid for cid in names if cid not in touched]

    # 考纲挂靠：图上"走到根"的另一条路（belongs_to → child_of，见 app/graph.py）。
    # 前置链断了只是"说不清先学哪个"；**没挂考纲则是在图上根本到不了根**。
    no_topic = [cid for cid, topic in concept_topics.items() if not topic]
    dangling = [
        cid for cid, topic in concept_topics.items() if topic and topic not in topics
    ]

    problems: list[dict] = []

    def flag(name: str, samples: list[str], hint: str) -> None:
        if samples:
            problems.append(
                {"name": name, "count": len(samples), "samples": samples[:limit], "hint": hint}
            )

    flag(
        "self_loops",
        [label(cid) for cid in self_loops],
        "自己指向自己 —— 删掉那条边",
    )
    flag(
        "cycles",
        [" → ".join(label(cid) for cid in cycle) for cycle in cycles],
        "环上的概念排不出先后（互为前置），先断哪条边由人定",
    )
    flag(
        "bidirectional",
        [f"{label(src)} ⇄ {label(dst)}（{kind}）" for src, dst, kind in bidirectional],
        "同一种有序关系双向成立 —— 只能留一个方向，方向约定见文件头",
    )
    flag(
        "kind_conflicts",
        [" ⇄ ".join(label(cid) for cid in sorted(pair)) for pair in kind_conflicts],
        "同一对概念既是前置又是组成 —— 留下一句更准的",
    )
    flag(
        "no_topic",
        [label(cid) for cid in no_topic],
        "没挂在任何考纲节点上 —— 图上到不了根（belongs_to → child_of 那条路是断的）",
    )
    flag(
        "dangling_topic",
        [f"{label(cid)} → {concept_topics[cid]}" for cid in dangling],
        "挂的考纲 key 不在考纲表里 —— 挂错了，或那个考纲节点被删了",
    )

    metrics = {
        "concepts": len(names),
        "ordered_edges": len(ordered),
        "concepts_with_ordered_edge": len(names) - len(unrooted),
        "roots": len(roots),
        "unrooted": len(unrooted),
        "concepts_with_topic": len(names) - len(no_topic),
        "max_chain": _longest_chain(prereqs),
        # 口径与 cc.md 一致：**边数 / 概念数**（686/912 ≈ 0.75），不是两端各算一次
        "ordered_per_concept": round(len(ordered) / len(names), 2) if names else 0.0,
    }
    return {"ok": not problems, "problems": problems, "metrics": metrics}


def _print_check(report: dict) -> None:
    m = report["metrics"]
    print(
        f"  概念 {m['concepts']} · 有序边 {m['ordered_edges']}"
        f"（平均 {m['ordered_per_concept']} 条/概念）"
    )
    print(
        f"  起点（没有前置）{m['roots']} · 一条有序边都没有 {m['unrooted']}"
        f" · 最长前置链 {m['max_chain']} 步"
    )
    if report["ok"]:
        print("  体检通过：无自环、无环、同一种有序关系不双向、闭包不矛盾")
        return
    print("  体检没通过：")
    for problem in report["problems"]:
        print(f"    ✗ {problem['name']} × {problem['count']} —— {problem['hint']}")
        for sample in problem["samples"]:
            print(f"        {sample}")


def stats() -> dict:
    """图谱体检：不只是"有多少"，还要能看出**算得对不对**。

    这张表要当所有下游算法的基石，所以这些数字必须随时看得见：
    有多少概念还没被题覆盖（该出题）、每个概念几道题（够不够）、
    归并把握度低的有哪些（该人看一眼）、有没有自环与双向语义边（该修的脏数据）。
    """
    # 体检结果并进来：这一眼既要看"有多少"，也要看"算得对不对"
    # （明细 —— 哪个环、哪对边双向 —— 用 `graph_build check` 看）
    report = check(limit=3)
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
            "edges": edges,
            "health": {
                "ok": report["ok"],
                "problems": [problem["name"] for problem in report["problems"]],
                **report["metrics"],
            },
        }


# --------------------------------------------------------------------------- 命令行


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.graph_build", description="知识图谱构建")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("merge", help="点归并成概念")
    sub.add_parser("edges", help="派生共现边")
    rel = sub.add_parser("relate", help="模型判语义关系")
    rel.add_argument("--limit", type=int, default=40)
    cen = sub.add_parser(
        "relate-centric", help="以概念为中心判前置（一次问一个概念 + 它的候选）"
    )
    cen.add_argument("--limit", type=int, default=20)
    # 默认从 2.0 降到 1.0：共现权重就是"一起出现在几个切片里"，现网 2288 条里
    # 2271 条是 1 —— 只喂 weight>=2 等于只判 17 对，补不动那张图。
    # 要"证据更硬"就显式传 --min-weight 2。
    rel.add_argument("--min-weight", type=float, default=1.0)
    sub.add_parser("stats", help="看一眼图现在什么样")
    chk = sub.add_parser("check", help="图谱体检：无环 / 能走到根 / 闭包不矛盾")
    chk.add_argument("--json", action="store_true", help="输出 JSON（给脚本与流水线用）")
    chk.add_argument("--limit", type=int, default=5, help="每类问题最多列几条")
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
    elif args.cmd == "relate-centric":
        print("以概念为中心判边：", asyncio.run(relate_centric(limit=args.limit)))
    elif args.cmd == "export":
        print("导出：", export(Path(args.out), min_weight=args.min_weight))
    elif args.cmd == "stats":
        for key, value in stats().items():
            print(f"  {key}: {value}")
    elif args.cmd == "check":
        report = check(limit=args.limit)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            _print_check(report)
        # 硬不变量破了就退出码非零 —— 这样它能直接串进流水线当闸门（见 Makefile 的 graph-check）
        return 0 if report["ok"] else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
