"""图谱的"日志化"与"考点化"：让图谱从"我们内部怎么想"变成算法能用的东西。

三件事，都是 THESIS §6 主张的落地：

1. ``assign_topics`` —— **概念归类到考纲**（考点化）。
   912 个概念里有近一半（碎片概念：1 点 1 材料）没有考纲归属，于是"每个考点
   出几道题"这件事没法算，覆盖率的分母也是虚的。这里用**同切片共现**做标签传播：
   碎片跟谁一起被抽出来，就跟着谁走 —— 依据是"它们在同一段原文里被一起讲过"，
   比让模型猜有据可查。

2. ``derive_from_artifacts`` —— **关系改回日志**（不是项目）。
   THESIS §6：关系应当由既有产出自动解析，零额外标注。
     * 选项辨析 → 易混边：一道题的选项里同时出现两个概念名，这两个就是易混关系。
     * 小问复用 → 前置边：大题的小问按序推进，前一问覆盖的概念是后一问的前置。
   模型判出来的边（`derived_by='llm'`）在这套体系里降级为**提议**：
   它仍然存着、仍然可看，但只有 `why` 说得清来源的边才算"日志"。

3. ``targets`` —— **出题目标**：每个考纲节点该有多少题、现在差多少。
   题量的权重来自"跨材料出现次数"——一个概念在 10 份材料里被讲，
   它就是通用概念（`tt-metal-circular-buffer` 那种），值得多出几道；
   只在一份材料里出现一次的，一道起步就够了。这既是出题优先级，
   也是"覆盖率"的真实分母。

命令行::

    api/.venv/bin/python -m pipeline.graph_log assign     # 概念归类到考纲
    api/.venv/bin/python -m pipeline.graph_log derive     # 从题目产物解析关系
    api/.venv/bin/python -m pipeline.graph_log targets    # 出题目标与缺口
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from sqlalchemy import text

from . import config

sys.path.insert(0, str(config.ROOT / "api"))


def _engine():
    from app.db import get_engine  # noqa: PLC0415

    return get_engine()


STOP_AT = ("## 答案", "## 解析", "## 答案与解析")
OPTION_RE = re.compile(r"^\s*(?:[-*]\s*)?([A-Z])[.、:：)]\s*(.+)$", re.MULTILINE)


def split_stem(markdown: str) -> str:
    """题干（不含答案与解析）—— 解析里提到概念不算"考它"。"""
    cut = len(markdown)
    for marker in STOP_AT:
        found = markdown.find(marker)
        if 0 <= found < cut:
            cut = found
    return markdown[:cut]


def options_of(markdown: str) -> list[str]:
    """选项行（形如 `- A. xxx`）。只有选择题才有。"""
    return [match.group(2).strip() for match in OPTION_RE.finditer(split_stem(markdown))]


# --------------------------------------------------------------------------- 考点化


def assign_topics(rounds: int = 4) -> dict:
    """未归属的概念跟着**同切片共现**的邻居归位（标签传播）。

    为什么这么做而不是让模型判、也不是让人一个个圈：依据是可查的 ——
    "这个碎片和这些已归位的概念在 N 个切片里一起被抽出来过"。
    人只需要复核结果（低权重的那些），不必从 487 个碎片开始圈。
    """
    with _engine().begin() as conn:
        concepts = {
            row[0]: {"key": row[1], "topic": row[2] or "", "materials": row[3]}
            for row in conn.execute(
                text("SELECT id, key, topic_key, material_count FROM concepts WHERE status <> 'retired'")
            )
        }
        neighbours: dict[int, list[tuple[int, float]]] = defaultdict(list)
        for row in conn.execute(
            text(
                "SELECT from_concept_id, to_concept_id, weight FROM concept_edges"
                " WHERE type = 'co_occurs'"
            )
        ):
            neighbours[row[0]].append((row[1], float(row[2])))
            neighbours[row[1]].append((row[0], float(row[2])))

        assigned_before = sum(1 for c in concepts.values() if c["topic"])
        newly = 0
        for _ in range(rounds):
            decided: dict[int, tuple[str, float]] = {}
            for cid, info in concepts.items():
                if info["topic"] or cid not in neighbours:
                    continue
                votes: Counter[str] = Counter()
                for other, weight in neighbours[cid]:
                    topic = concepts.get(other, {}).get("topic")
                    if topic:
                        votes[topic] += weight
                if not votes:
                    continue
                topic, score = votes.most_common(1)[0]
                decided[cid] = (topic, score)
            if not decided:
                break
            # 一轮只落库一次：同一轮里互相投票会把噪声滚起来
            for cid, (topic, _score) in decided.items():
                concepts[cid]["topic"] = topic
                conn.execute(
                    text(
                        "UPDATE concepts SET topic_key = :topic, updated_at = now()"
                        " WHERE id = :id AND topic_key = ''"
                    ),
                    {"topic": topic, "id": cid},
                )
                newly += 1

        # 剩下的看题：覆盖它的题挂在哪个考纲节点上（题目的 topic 是人写的）
        for row in conn.execute(
            text(
                """
                SELECT qc.concept_id, q.topic, COUNT(*) n
                FROM question_concepts qc JOIN questions q ON q.id = qc.question_id
                WHERE q.retired_at IS NULL AND q.topic <> ''
                GROUP BY 1, 2
                """
            )
        ):
            if not concepts.get(row[0], {}).get("topic"):
                curated = conn.execute(
                    text("SELECT 1 FROM topics WHERE key = :k"), {"k": row[1]}
                ).scalar()
                if curated:
                    concepts[row[0]]["topic"] = row[1]
                    conn.execute(
                        text(
                            "UPDATE concepts SET topic_key = :topic, updated_at = now()"
                            " WHERE id = :id AND topic_key = ''"
                        ),
                        {"topic": row[1], "id": row[0]},
                    )
                    newly += 1

        left = conn.execute(
            text("SELECT COUNT(*) FROM concepts WHERE topic_key = '' AND status <> 'retired'")
        ).scalar()
        total = len(concepts)
    return {
        "concepts": total,
        "assigned_before": assigned_before,
        "newly_assigned": newly,
        "still_unassigned": left,
    }


# --------------------------------------------------------------------------- 日志式关系


def derive_from_artifacts(limit: int = 0) -> dict:
    """从题目产物解析关系（零额外标注）。

    两类信号，都能指名道姓说出依据：

    * **易混**：同一道题的选项里出现了两个概念名 —— 出题人把它们放在一起当干扰项，
      这本身就是"这两件事容易混"的证据（`why` 记题号）。
    * **前置**：大题的小问按序推进 —— 前一问覆盖的概念是后一问的前置
      （`why` 记题号与问序）。

    写进去时 `derived_by='code'`，与人确认过的（`human`）、模型提议的（`llm`）
    分开存，前端与算法可以按来源区分。
    """
    with _engine().begin() as conn:
        concepts = [
            (row[0], row[1], (row[2] or "").strip())
            for row in conn.execute(
                text("SELECT id, key, name FROM concepts WHERE status <> 'retired'")
            )
        ]
        # 概念名是**长句**（"Tracy 埋点宏：ZoneScoped / FrameMarkNamed"），
        # 选项里永远不会整句出现 —— 按整名匹配一条边都出不来（实测如此）。
        # 所以按**稀有词**匹配：把 key 与名字切成词，只留"在整个概念集里只出现 ≤3 次"的词
        # （tracy / allclose / circular / 埋点 这种），命中一个稀有词就认定指向该概念。
        token_re = re.compile(r"[a-z_][a-z0-9_]{3,}|[\u4e00-\u9fff]{2,}")
        token_owner: dict[str, set[int]] = defaultdict(set)
        names = {cid: (name or key) for cid, key, name in concepts}
        concept_ids = [(cid, key, name) for cid, key, name in concepts]
        for cid, key, name in concepts:
            for token in set(token_re.findall(f"{key} {name}".lower())):
                token_owner[token].add(cid)
        # 门槛别太严：sharded / mcast / interleaved 这类词在这个域里出现在几十个概念里，
        # 一刀切成"通用词"就什么也匹配不上（实测一条边都出不来）。
        # 办法改成"同一个概念要命中 ≥2 个词才算"：极稀有的词一个就够，
        # 稍常见的词要两个同时出现 —— 精度靠"多词共现"而不是靠"词有多罕见"。
        MAX_OWNERS = 20

        questions = conn.execute(
            text(
                "SELECT q.id, q.type, q.raw_markdown FROM questions q"
                " WHERE q.retired_at IS NULL AND q.status = 'published'"
                + (f" LIMIT {int(limit)}" if limit else "")
            )
        ).all()
        covers = defaultdict(list)
        for row in conn.execute(
            text("SELECT question_id, concept_id FROM question_concepts")
        ):
            covers[row[0]].append(row[1])

        pairs: dict[tuple[int, int, str], str] = {}
        for qid, qtype, markdown in questions:
            ids = covers.get(qid) or []
            if not ids:
                continue
            # 注意：**不能**要求"这道题自己覆盖 ≥2 个概念" —— 单选题通常只考一个概念，
            # 而"易混"的证据恰恰来自**干扰项**（别的概念被摆进来当干扰项）。
            stem = split_stem(markdown or "")
            opts = " ".join(options_of(markdown or "")).lower()

            def hits(fragment: str, pool: list[int]) -> list[int]:
                """fragment 里命中哪些概念。

                极稀有的词（≤3 个概念拥有）命中一个就够；稍常见的词要两个同时出现 ——
                精度靠"多词共现"，而不是靠"词有多罕见"（这个域里的技术词几乎都很常见）。
                """
                poolset = set(pool)
                score: Counter[int] = Counter()
                for token in set(token_re.findall(fragment.lower())):
                    owners = token_owner.get(token)
                    if not owners:
                        continue
                    shared = owners & poolset
                    if not shared:
                        continue
                    weight = 2 if len(owners) <= 3 else (1 if len(owners) <= MAX_OWNERS else 0)
                    for cid in shared:
                        score[cid] += weight
                return [cid for cid, n in score.items() if n >= 2]

            # 易混：一道题的**干扰项**里出现了两个"其它概念" —— 出题人把它们和正确答案
            # 摆在一起，这就是"这几件事容易混"的证据。
            # 注意不能用"这道题自己考的概念"：单选题通常只覆盖一个概念，
            # 那样配对永远凑不齐两个（实测一条边都出不来）。
            all_ids = [row[0] for row in concept_ids]
            in_options = [cid for cid in hits(opts, all_ids) if cid not in ids]
            for i in range(len(in_options)):
                for j in range(i + 1, len(in_options)):
                    a, b = in_options[i], in_options[j]
                    pairs[(a, b, "contrast_with")] = f"选项辨析：题 {qid} 把它们放在一起当干扰项"
            # 前置：按题干里首次出现的顺序，先讲的是后考的前置（大题的小问按序推进）
            if qtype == "problem":
                order = []
                low = stem.lower()
                for cid in ids:
                    name = names.get(cid, "").lower()
                    pos = low.find(name) if name else -1
                    if pos >= 0:
                        order.append((pos, cid))
                order.sort()
                for (_, a), (_, b) in zip(order, order[1:]):
                    if a != b:
                        pairs[(a, b, "requires")] = f"小问复用：题 {qid} 里先讲后考"

        written = 0
        for (a, b, etype), why in pairs.items():
            result = conn.execute(
                text(
                    "INSERT INTO concept_edges (from_concept_id, to_concept_id, type, why,"
                    " derived_by, weight) VALUES (:a, :b, :t, :why, 'code', 1.0)"
                    " ON CONFLICT (from_concept_id, to_concept_id, type) DO NOTHING"
                ),
                {"a": a, "b": b, "t": etype, "why": why},
            )
            written += result.rowcount or 0

        counts = {
            row[0]: row[1]
            for row in conn.execute(
                text("SELECT derived_by, COUNT(*) FROM concept_edges GROUP BY 1")
            )
        }
    return {"questions_scanned": len(questions), "written": written, "by_source": counts}


# --------------------------------------------------------------------------- 出题目标


def targets() -> dict:
    """每个考纲节点该有多少题、现在差多少 —— 出题机的派工依据。

    权重规则（简单、可解释）：
        概念目标题量 = 3 + min(3, 跨材料出现次数 - 1)
        跨 1 份材料 → 3 道；2 份 → 4 道；4 份以上 → 6 道
    为什么至少 3 道：自适应要有**选项**。只有一道题时，"换个角度再考一次""做错了换一道
    同概念的题"都无从谈起；3 道才够覆盖一个概念的几个侧面，也够组卷时不重复。
    为什么按跨材料次数加权：那东西有多通用，就该多练几面 —— 在 10 份材料里被讲的东西，
    是判断链上的公共环节。
    """
    with _engine().begin() as conn:
        rows = conn.execute(
            text(
                """
                SELECT COALESCE(NULLIF(c.topic_key, ''), '(待归类)') topic,
                       COUNT(*) concepts,
                       SUM(CASE WHEN c.question_count > 0 THEN 1 ELSE 0 END) covered,
                       SUM(3 + LEAST(3, GREATEST(0, c.material_count - 1))) target,
                       SUM(c.question_count) current
                FROM concepts c WHERE c.status <> 'retired'
                GROUP BY 1 ORDER BY (SUM(3 + LEAST(3, GREATEST(0, c.material_count - 1))) - SUM(c.question_count)) DESC
                """
            )
        ).all()
        name_of = {
            row[0]: row[1] or row[0]
            for row in conn.execute(text("SELECT key, name FROM topics"))
        }
        out = []
        for topic, concepts, covered, target, current in rows:
            out.append(
                {
                    "topic": topic,
                    "name": name_of.get(topic, topic),
                    "concepts": concepts,
                    "covered": covered,
                    "target": target,
                    "current": current,
                    "gap": max(0, target - current),
                }
            )
        return {"nodes": out, "total_gap": sum(item["gap"] for item in out)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.graph_log", description="图谱日志化与考点化")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("assign", help="概念归类到考纲（标签传播 + 题目佐证）")
    der = sub.add_parser("derive", help="从题目产物解析关系（易混 / 前置）")
    der.add_argument("--limit", type=int, default=0)
    tgt = sub.add_parser("targets", help="出题目标与缺口")
    tgt.add_argument("--top", type=int, default=12)

    args = parser.parse_args(argv)
    if args.cmd == "assign":
        print("考点化：", assign_topics())
    elif args.cmd == "derive":
        print("日志式关系：", derive_from_artifacts(limit=args.limit))
    elif args.cmd == "targets":
        data = targets()
        print(f"出题缺口合计 {data['total_gap']} 道 —— 缺口最大的考纲节点：")
        for item in data["nodes"][: args.top]:
            print(
                "  %-34s %3d 概念（覆盖 %3d）· 现在 %3d / 目标 %3d · 缺 %3d"
                % (item["name"][:34], item["concepts"], item["covered"],
                   item["current"], item["target"], item["gap"])
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
