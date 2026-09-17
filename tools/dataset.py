#!/usr/bin/env python3
"""题库数据集的装配。

**导入与出口共用这一份**，这是刻意的：

  * 导入：`api/` 的导入器把它写进库
  * 出口：`GET /api/bank` 与 `pipeline.bankfile export` 把它原样吐出来

同一份 JSON 既写库又读出来，结构必须一致；顺序、聚合方式、字段名
只能在这里定义一次 —— 否则会出现「库里是一套、前端拿到的是另一套」，
表现成某块界面静默空掉，极难定位。
"""

from __future__ import annotations

import datetime as _dt

from question_parser import TYPE_LABELS
from topics import load as _load_topics


def load_topic_tree() -> tuple[dict[str, dict], list[str], list[dict], list[str]]:
    """解析考纲主题树（学科 → 单元 → 知识点）。

    返回 (nodes, ordered, groups, problems)。problems 交给调用方决定
    怎么报（构建期走日志，导入期走诊断），这里不打印也不抛。
    """
    return _load_topics()


def build_dataset(
    questions: list[dict],
    nodes: dict[str, dict],
    ordered: list[str],
    groups_raw: list[dict],
) -> dict:
    # 节点按深度优先顺序输出（父在子前），前端直接按这个顺序渲染三级筛选器
    topic_list = [
        {
            "key": nodes[key]["key"],
            "name": nodes[key]["name"],
            "group": nodes[key]["group"],
            "order": nodes[key]["order"],
            "color": nodes[key]["color"] or "#2DD4BF",
            "desc": nodes[key]["desc"],
            "depth": nodes[key]["depth"],
            "parent": nodes[key]["parent"],
            "path": nodes[key]["path"],
            "pathNames": nodes[key]["pathNames"],
            "children": nodes[key]["children"],
            "descendants": nodes[key]["descendants"],
            "leaf": nodes[key]["leaf"],
        }
        for key in ordered
    ]

    groups = sorted(groups_raw, key=lambda g: g["order"])
    for group in groups:
        group["topics"] = [t["key"] for t in topic_list if t["depth"] == 1 and t["group"] == group["key"]]
    # 空分组不发出去。接口侧（current_bank）是按"真有节点"组出来的，
    # 两条产出路径的分组必须一致 —— 否则前端会多画一个空分区。
    # 实测撞过：删掉凑数学科之后，只有文件侧还在发那三个空分组。
    groups = [g for g in groups if g["topics"]]

    questions = sorted(
        questions,
        key=lambda q: (ordered.index(q["topic"]) if q["topic"] in ordered else 9999, q["id"]),
    )

    # 每个节点都给一个 0，即使一道题都没有。
    # 否则前端的求和 / 统计要到处写 `|| 0`，漏一处就是 NaN。
    by_topic: dict[str, int] = {key: 0 for key in ordered}
    by_type: dict[str, int] = {}
    by_difficulty: dict[str, int] = {}
    for q in questions:
        # 题目的计数沿着 path 累加到每一级祖先：这样学科卡片上的题数 =
        # 其下所有知识点的题数之和，前端不必再自己聚合。
        for key in nodes.get(q["topic"], {}).get("path", [q["topic"]]):
            by_topic[key] = by_topic.get(key, 0) + 1
        by_type[q["type"]] = by_type.get(q["type"], 0) + 1
        diff = str(q["difficulty"])
        by_difficulty[diff] = by_difficulty.get(diff, 0) + 1

    return {
        "meta": {
            "generator": "quizforge",
            "version": 2,
            "generatedAt": _dt.datetime.now().isoformat(timespec="seconds"),
            "topics": topic_list,
            "groups": groups,
            "typeLabels": TYPE_LABELS,
            "stats": {
                "total": len(questions),
                "byTopic": by_topic,
                "byType": by_type,
                "byDifficulty": by_difficulty,
                # 学科数量：首页与筛选器的一级只显示这么多
                "subjectCount": sum(1 for t in topic_list if t["depth"] == 1),
            },
        },
        "questions": questions,
    }
