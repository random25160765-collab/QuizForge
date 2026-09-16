"""考纲（原先的 `meta/topics.yaml`）：**库内为正**，YAML 只是导出物。

原来是"文件为为正、库为副本"：人编辑 YAML，构建期读 YAML，导入时把它抄进 `topics` 表。
树一旦变大（学科 × 单元 × 知识点，还会跨材料、跨语言），这套就不成立了：

* 每次改动都要重写整份文件 —— 两处同时改，必然互相覆盖；
* 答不了"这个节点是什么时候加的、因为哪道题加的"；
* 构建、校验、后端各读一遍文件，天然的漂移源。

所以反过来：**数据库是权威**，YAML 由这里渲染出去（构建与校验工具读的是
`make bank-materialize` 生成的那一份临时文件）。本模块提供两件事：

* `render_yaml()`：树 → YAML 文本（供工具链与快照用）；
* 按**节点**增删改（`add_node` / `rename_node` / `move_node` / `retire_node`），
  并自动维护深度、路径、子节点、后代与叶子标记 —— 树大了以后，
  这正是文件做不到、而数据库最擅长的事。
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text

from .db import get_engine

MAX_DEPTH = 3  # 学科 → 单元 → 知识点（与 tools/topics.py 一致）


def _load(conn, include_retired: bool = False) -> list[dict]:
    # ``desc`` 是 SQL 保留字，裸写会直接语法错（实测踩过），一律加引号
    sql = (
        'SELECT key, name, group_key, order_index, color, "desc", depth, parent_key, is_leaf'
        " FROM topics"
    )
    if not include_retired:
        sql += " WHERE retired_at IS NULL"
    sql += " ORDER BY order_index, key"
    return [dict(row._mapping) for row in conn.execute(text(sql))]


def groups(conn=None) -> list[dict]:
    def run(connection) -> list[dict]:
        return [
            dict(row._mapping)
            for row in connection.execute(
                text("SELECT key, name, order_index FROM topic_groups ORDER BY order_index, key")
            )
        ]

    if conn is not None:
        return run(conn)
    with get_engine().connect() as own:
        return run(own)


def _quote(value: Any) -> str:
    """YAML 标量：数字裸写，字符串统一双引号（YAML 的超集，解析器都收）。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def render_yaml(conn=None) -> str:
    """树 → `topics.yaml` 文本（分组 + 树，逐节点按 order 排序）。"""
    if conn is not None:
        nodes, group_rows = _load(conn), groups(conn)
    else:
        with get_engine().connect() as own:
            nodes, group_rows = _load(own), groups(own)

    children: dict[str | None, list[dict]] = {}
    for node in nodes:
        children.setdefault(node["parent_key"] or None, []).append(node)

    lines: list[str] = ["groups:"]
    for group in group_rows:
        lines.append(f"  - key: {_quote(group['key'])}")
        lines.append(f"    name: {_quote(group['name'])}")
        lines.append(f"    order: {group['order_index']}")
    if not group_rows:
        lines.append("  []")

    lines.append("topics:")

    def emit(node: dict, indent: int) -> None:
        pad = " " * indent
        lines.append(f"{pad}- key: {_quote(node['key'])}")
        lines.append(f"{pad}  name: {_quote(node['name'])}")
        if node["depth"] == 1:
            if node["group_key"]:
                lines.append(f"{pad}  group: {_quote(node['group_key'])}")
            if node["color"]:
                lines.append(f"{pad}  color: {_quote(node['color'])}")
        lines.append(f"{pad}  order: {node['order_index']}")
        if node["desc"]:
            lines.append(f"{pad}  desc: {_quote(node['desc'])}")
        kids = children.get(node["key"]) or []
        if kids:
            lines.append(f"{pad}  children:")
            for kid in kids:
                emit(kid, indent + 4)

    for root in children.get(None) or []:
        emit(root, 2)
    return "\n".join(lines) + "\n"


def _recompute(conn) -> None:
    """按 `parent_key` 重算父子关系、深度、路径、后代与叶子标记（确定性、可重放）。"""
    rows = [
        dict(row._mapping)
        for row in conn.execute(
            text(
                "SELECT key, name, parent_key, order_index, is_leaf FROM topics"
                " WHERE retired_at IS NULL"
            )
        )
    ]
    by_parent: dict[str | None, list[str]] = {}
    by_key = {row["key"]: row for row in rows}
    for row in rows:
        by_parent.setdefault(row["parent_key"] or None, []).append(row["key"])

    path: dict[str, list[str]] = {}
    path_names: dict[str, list[str]] = {}
    depth: dict[str, int] = {}
    descendants: dict[str, list[str]] = {}
    children_map: dict[str, list[str]] = {}

    def walk(key: str, parent: str | None, level: int) -> list[str]:
        node = by_key[key]
        depth[key] = level
        path[key] = (path.get(parent, []) if parent else []) + [key]
        path_names[key] = (path_names.get(parent, []) if parent else []) + [node["name"]]
        kids = by_parent.get(key) or []
        children_map[key] = sorted(kids)
        found: list[str] = []
        for kid in kids:
            found.extend(walk(kid, key, level + 1))
            found.append(kid)
        descendants[key] = found
        return found

    for root in by_parent.get(None) or []:
        walk(root, None, 1)

    for key in by_key:
        conn.execute(
            text(
                """
                UPDATE topics SET depth = :depth, path = CAST(:path AS JSONB),
                  path_names = CAST(:names AS JSONB), children = CAST(:children AS JSONB),
                  descendants = CAST(:descendants AS JSONB), is_leaf = :leaf, updated_at = now()
                WHERE key = :key
                """
            ),
            {
                "depth": depth[key],
                "path": json.dumps(path[key], ensure_ascii=False),
                "names": json.dumps(path_names[key], ensure_ascii=False),
                "children": json.dumps(children_map[key], ensure_ascii=False),
                "descendants": json.dumps(descendants[key], ensure_ascii=False),
                "leaf": not children_map[key],
                "key": key,
            },
        )


def _existing(conn, key: str) -> dict | None:
    row = conn.execute(
        text("SELECT key, name, parent_key, depth, order_index, retired_at FROM topics WHERE key = :key"),
        {"key": key},
    ).fetchone()
    return dict(row._mapping) if row else None


def add_node(
    key: str,
    name: str,
    parent: str | None = None,
    order: int | None = None,
    color: str = "",
    desc: str = "",
    group: str = "",
) -> dict:
    """新增一个节点（学科 / 单元 / 知识点）。父节点不存在就报错，不猜。"""
    with get_engine().begin() as conn:
        if _existing(conn, key):
            raise ValueError(f"节点已存在：{key}")
        level = 1
        if parent:
            parent_node = _existing(conn, parent)
            if parent_node is None:
                raise ValueError(f"父节点不存在：{parent}")
            level = int(parent_node["depth"]) + 1
        if level > MAX_DEPTH:
            raise ValueError(f"层级超过 {MAX_DEPTH} 级（学科 → 单元 → 知识点）")
        if level == 1 and not color:
            raise ValueError("一级学科必须给 --color（界面按学科上色）")
        if level > 1 and not group and parent:
            row = conn.execute(
                text("SELECT group_key, color FROM topics WHERE key = :key"), {"key": parent}
            ).fetchone()
            group = group or (row[0] if row else "")
            color = color or (row[1] if row else "")
        if order is None:
            row = conn.execute(
                text(
                    "SELECT COALESCE(MAX(order_index), 0) + 10 FROM topics"
                    " WHERE parent_key IS NOT DISTINCT FROM :parent"
                ),
                {"parent": parent},
            ).fetchone()
            order = int(row[0])
        conn.execute(
            text(
                """
                INSERT INTO topics (key, name, group_key, order_index, color, "desc", depth,
                                    parent_key, path, path_names, children, descendants, is_leaf)
                VALUES (:key, :name, :group, :order, :color, :desc, :depth, :parent,
                        '[]'::jsonb, '[]'::jsonb, '[]'::jsonb, '[]'::jsonb, true)
                """
            ),
            {
                "key": key,
                "name": name,
                "group": group,
                "order": order,
                "color": color,
                "desc": desc,
                "depth": level,
                "parent": parent,
            },
        )
        _recompute(conn)
    return {"key": key, "name": name, "parent": parent, "depth": level, "order": order}


def update_node(key: str, **fields: Any) -> dict:
    """改节点：`name` / `order` / `color` / `desc` / `group` / `parent`（换父 = 搬家）。"""
    allowed = {"name", "order_index", "color", "desc", "group_key", "parent_key"}
    patch = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not patch:
        raise ValueError("没有要改的字段")
    with get_engine().begin() as conn:
        if _existing(conn, key) is None:
            raise ValueError(f"节点不存在：{key}")
        if "parent_key" in patch and patch["parent_key"]:
            parent_node = _existing(conn, patch["parent_key"])
            if parent_node is None:
                raise ValueError(f"新父节点不存在：{patch['parent_key']}")
            if key in (parent_node["path"] if isinstance(parent_node, dict) else []) or key == patch["parent_key"]:
                raise ValueError("不能把节点搬到它自己或它的后代下")
        sets = ", ".join(f'"{name}" = :{name}' for name in patch)
        conn.execute(
            text(f"UPDATE topics SET {sets}, updated_at = now() WHERE key = :key"),
            {**patch, "key": key},
        )
        _recompute(conn)
    return {"key": key, **patch}


def retire_node(key: str, cascade: bool = True) -> dict:
    """退役节点（**不删除**：题目的 `topic` 指向它，进度与统计还要能回看）。

    默认连同子树一起退役；子树里有题的话，调用方应当先把题归到别处。
    """
    with get_engine().begin() as conn:
        node = _existing(conn, key)
        if node is None:
            raise ValueError(f"节点不存在：{key}")
        keys = [key]
        if cascade:
            rows = conn.execute(
                text("SELECT key FROM topics WHERE path @> CAST(:path AS JSONB)"),
                {"path": json.dumps([key])},
            ).fetchall()
            keys = sorted({key, *(row[0] for row in rows)})
        conn.execute(
            text("UPDATE topics SET retired_at = now(), updated_at = now() WHERE key = ANY(:keys)"),
            {"keys": keys},
        )
        _recompute(conn)
    return {"retired": keys}


def list_nodes(parent: str | None = None, depth: int | None = None) -> list[dict]:
    sql = "SELECT key, name, depth, parent_key, order_index, is_leaf FROM topics WHERE retired_at IS NULL"
    args: dict[str, Any] = {}
    if parent:
        sql += " AND parent_key = :parent"
        args["parent"] = parent
    if depth:
        sql += " AND depth = :depth"
        args["depth"] = depth
    sql += " ORDER BY depth, order_index, key"
    with get_engine().connect() as conn:
        return [dict(row._mapping) for row in conn.execute(text(sql), args)]


def upsert_groups(definitions: list[dict]) -> int:
    """写入分组定义（导入 YAML 时用）。"""
    if not definitions:
        return 0
    with get_engine().begin() as conn:
        for item in definitions:
            conn.execute(
                text(
                    "INSERT INTO topic_groups (key, name, order_index) VALUES (:key, :name, :order)"
                    " ON CONFLICT (key) DO UPDATE SET name = EXCLUDED.name,"
                    " order_index = EXCLUDED.order_index"
                ),
                {"key": item["key"], "name": item.get("name") or item["key"], "order": int(item.get("order", 999))},
            )
    return len(definitions)
