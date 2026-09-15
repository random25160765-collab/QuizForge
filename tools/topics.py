"""考纲主题树的解析与校验。

`meta/topics.yaml` 是考纲的唯一事实来源，这里把它摊平成「节点表」供
`build.py`（写进产物给前端）与 `check.py`（校验题目的 topic 是否存在）共用。
两处各写一份解析迟早会漂移，所以只留这一份。

三级结构：学科(depth=1) → 单元(depth=2) → 知识点(depth=3)。
题目可以挂任意一层；按上层筛选时会包含其全部子孙，因此每个节点都带
`descendants`（不含自己），前端不必自己爬树。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
TOPICS_FILE = ROOT / "meta" / "topics.yaml"

MAX_DEPTH = 3


class TopicError(Exception):
    """topics.yaml 结构有问题。"""


def _text(value: Any, fallback: str = "") -> str:
    if value is None:
        return fallback
    return str(value).strip() or fallback


def load() -> tuple[dict[str, dict], list[str], list[dict], list[str]]:
    """解析 topics.yaml。

    返回 (nodes, ordered_keys, groups, problems)：
      nodes        : key -> 扁平节点（含 depth/parent/path/pathNames/children/descendants/leaf）
      ordered_keys : 深度优先顺序，父在子前，稳定性由 order/声明顺序共同决定
      groups       : [{key, name, order}]
      problems     : 校验发现的问题（人性化描述），调用方决定是报错还是告警
    """
    try:
        import yaml  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise TopicError("需要 PyYAML 才能解析 meta/topics.yaml") from exc

    if not TOPICS_FILE.is_file():
        raise TopicError(f"找不到 {TOPICS_FILE}")

    raw = yaml.safe_load(TOPICS_FILE.read_text(encoding="utf-8")) or {}
    groups = []
    for item in raw.get("groups", []) or []:
        key = _text(item.get("key"))
        if key:
            groups.append({"key": key, "name": _text(item.get("name"), key), "order": int(item.get("order", 999))})
    group_keys = {g["key"] for g in groups}

    nodes: dict[str, dict] = {}
    ordered: list[str] = []
    problems: list[str] = []

    def walk(items: list, depth: int, parent: dict | None) -> list[str]:
        children: list[str] = []
        for item in items or []:
            if not isinstance(item, dict):
                problems.append("topics 下的每一项都必须是映射（key/name/children）")
                continue
            key = _text(item.get("key"))
            if not key:
                problems.append(f"{parent['key'] + ' 下' if parent else '顶层'}有一项缺少 key")
                continue
            if key in nodes:
                problems.append(f"topic key 重复：{key}")
                continue
            if depth > MAX_DEPTH:
                problems.append(f"{key} 层级超过 {MAX_DEPTH} 级（学科 → 单元 → 知识点）")
                continue

            if depth == 1:
                group = _text(item.get("group"))
                if group and group not in group_keys:
                    problems.append(f"{key} 的 group「{group}」没有在 groups 里定义")
                color = _text(item.get("color"))
                if not color:
                    problems.append(f"一级学科 {key} 缺少 color")
            else:
                # 子级默认继承：分组取顶层，颜色取顶层
                group = parent["group"] if parent else ""
                color = _text(item.get("color")) or (parent["color"] if parent else "")

            node = {
                "key": key,
                "name": _text(item.get("name"), key),
                "group": group,
                "order": int(item.get("order", 999)),
                "color": color,
                "desc": _text(item.get("desc")),
                "depth": depth,
                "parent": parent["key"] if parent else None,
                "path": (parent["path"] if parent else []) + [key],
                "pathNames": (parent["pathNames"] if parent else []) + [_text(item.get("name"), key)],
                "children": [],
                "descendants": [],
                "leaf": True,
            }
            nodes[key] = node
            ordered.append(key)

            child_keys = walk(item.get("children") or [], depth + 1, node)
            node["children"] = child_keys
            if child_keys:
                node["leaf"] = False
            children.append(key)
        return children

    walk(raw.get("topics") or [], 1, None)

    # 子孙在全部节点建好之后再算，避免中途依赖还没解析到的分支
    for key in ordered:
        acc: list[str] = []
        stack = list(nodes[key]["children"])
        while stack:
            child = stack.pop(0)
            acc.append(child)
            stack = list(nodes[child]["children"]) + stack
        nodes[key]["descendants"] = acc

    # 同级按 order、再按声明顺序排列，保证前后端顺序一致。
    # declared 保留「解析顺序」，不能被最终输出顺序覆盖。
    declared = list(ordered)
    ordered = []

    def sort_siblings(keys: list[str]) -> list[str]:
        return sorted(keys, key=lambda k: (nodes[k]["order"], declared.index(k)))

    def emit(keys: list[str]) -> None:
        for key in sort_siblings(keys):
            ordered.append(key)
            emit(nodes[key]["children"])

    emit([k for k in declared if nodes[k]["depth"] == 1])

    return nodes, ordered, sorted(groups, key=lambda g: g["order"]), problems


def resolve(nodes: dict[str, dict], key: str) -> dict | None:
    """把题目声明的 topic 解析成节点；未知 key 返回 None。"""
    return nodes.get(_text(key))
