"""画布（JSON Canvas）：读、校验、改动、写回。

画布是**一等条目**（`notelib` 里 `.canvas` 进索引、树里带 ◇ 显示），它的权威同样是文件 ——
Obsidian 的 JSON Canvas 格式，原样保留、不认识的多余字段一个字节都不删（将来它加了东西，
我们的读写不该把它吃掉）。

三条讲究：

* **容错优先**。真实语料里既有 2 字节的 `{}`（实测 `Untitled.canvas`）、也可能有写坏一半的
  JSON —— 读的时候一律给个空画布，绝不抛给用户。
* **改动是"一批操作"**。拖一张卡片是几十次位移，如果一次一次存，快照会被冲干净、
  历史也就废了。所以接口收一批 ops、校验完一起落盘，**只留一个版本**。
* **校验在写之前**。删节点必须连带删掉挂在它身上的边（否则留下指向空气的边，渲染时炸）、
  边两头的节点必须真的存在、尺寸不能是负数 —— 这些都在这里挡掉，前端的 bug 不该写进文件。
"""

from __future__ import annotations

import json
import math
import secrets
from pathlib import Path
from typing import Any

from . import notelib

#: 边的四个吸附方位。JSON Canvas 的取值，不自己发明。
SIDES = ("top", "right", "bottom", "left")

#: 节点类型 → 新建时的默认尺寸。`group` 是分组框，给大一点。
DEFAULT_SIZE = {
    "text": (250, 60),
    "file": (400, 240),
    "link": (250, 60),
    "group": (420, 320),
}

#: 尺寸下限：比这更小的卡片点不中也看不见，属于误操作
MIN_SIZE = (60, 40)

#: 画布能接受的最大坐标绝对值。真实语料里有 -17640 这种，留足空间；但拒绝 inf/天文数字。
COORD_LIMIT = 1_000_000


class CanvasError(Exception):
    """画布层面的错误（交给路由转成 400）。"""


# ------------------------------------------------------------------ 读


def read(lib: notelib.Library, rel: str) -> dict[str, Any]:
    """读一块画布。**任何解析失败都降级成空画布**，并把原因带出去。"""
    path = notelib.safe_path(lib, rel)
    if not path.is_file():
        raise CanvasError(f"没有这块画布：{rel}")
    raw = path.read_text(encoding="utf-8", errors="replace")
    broken = ""
    data: dict[str, Any] = {}
    try:
        loaded = json.loads(raw) if raw.strip() else {}
        if isinstance(loaded, dict):
            data = loaded
        else:
            broken = "顶层不是对象"
    except json.JSONDecodeError as exc:
        broken = f"JSON 坏了：{exc.msg}"
    normalized = normalize(data)
    normalized["path"] = rel
    normalized["lib"] = lib.name
    normalized["broken"] = broken
    normalized["bytes"] = len(raw.encode("utf-8"))
    # 包围盒一并给出：前端"适应窗口"全靠它 —— 内容坐标可能是 -17640，
    # 没有它就只能在原点附近瞎猜（打开就是一片空白）。
    normalized["bbox"] = bbox(normalized)
    return normalized


def normalize(data: dict[str, Any]) -> dict[str, Any]:
    """把画布拉成我们敢用的形状。

    只做**保证渲染不崩**的事：丢掉没有 id 的节点、把数值兜进范围、删掉指向不存在节点的边。
    不认识的多余字段原样留在节点/边里 —— 写回时它们还在。
    """
    raw_nodes = data.get("nodes") if isinstance(data.get("nodes"), list) else []
    nodes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_nodes:
        if not isinstance(item, dict):
            continue
        node_id = str(item.get("id") or "").strip()
        if not node_id or node_id in seen:
            continue
        seen.add(node_id)
        kind = str(item.get("type") or "text")
        if kind not in DEFAULT_SIZE:
            kind = "text"
        width, height = DEFAULT_SIZE[kind]
        node = dict(item)
        node["id"] = node_id
        node["type"] = kind
        node["x"] = _num(item.get("x"), 0)
        node["y"] = _num(item.get("y"), 0)
        node["width"] = max(MIN_SIZE[0], _num(item.get("width"), width))
        node["height"] = max(MIN_SIZE[1], _num(item.get("height"), height))
        nodes.append(node)

    by_id = {node["id"]: node for node in nodes}
    raw_edges = data.get("edges") if isinstance(data.get("edges"), list) else []
    edges: list[dict[str, Any]] = []
    seen_edges: set[str] = set()
    for item in raw_edges:
        if not isinstance(item, dict):
            continue
        source, target = str(item.get("fromNode") or ""), str(item.get("toNode") or "")
        if source not in by_id or target not in by_id or source == target:
            continue
        edge_id = str(item.get("id") or "").strip() or new_id()
        if edge_id in seen_edges:
            edge_id = new_id()
        seen_edges.add(edge_id)
        edge = dict(item)
        edge["id"] = edge_id
        edge["fromNode"] = source
        edge["toNode"] = target
        for key in ("fromSide", "toSide"):
            if edge.get(key) not in SIDES:
                edge.pop(key, None)
        edges.append(edge)

    return {"nodes": nodes, "edges": edges}


def bbox(data: dict[str, Any]) -> dict[str, float]:
    """内容包围盒。前端用它做"适应窗口" —— 坐标可以是负几万，没有它就只能瞎猜。"""
    nodes = data.get("nodes") or []
    if not nodes:
        return {"x": 0, "y": 0, "width": 0, "height": 0}
    left = min(float(node["x"]) for node in nodes)
    top = min(float(node["y"]) for node in nodes)
    right = max(float(node["x"]) + float(node["width"]) for node in nodes)
    bottom = max(float(node["y"]) + float(node["height"]) for node in nodes)
    return {"x": left, "y": top, "width": right - left, "height": bottom - top}


# ------------------------------------------------------------------ 改


def new_id() -> str:
    """与 Obsidian 同形状的 16 位十六进制 id（不要求全局唯一，只要在同一块画布内不撞）。"""
    return secrets.token_hex(8)


def _num(value: Any, fallback: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if math.isnan(number) or math.isinf(number) or abs(number) > COORD_LIMIT:
        return fallback
    return round(number, 2)


def free_slot(data: dict[str, Any], width: float, height: float) -> tuple[float, float]:
    """给新卡片找个不压别人的位置。

    规则简单到可以预期：摆在内容包围盒**右侧留一条缝**的地方，往下叠；
    压到别人就继续往右挪。比"永远创建在原点"好 —— 那会让新卡片全叠在一起，
    用户第一件事就是拖开它们。
    """
    box = bbox(data)
    if not data.get("nodes"):
        return (-width / 2, -height / 2)          # 空画布：摆在视口中心
    x = round(box["x"] + box["width"] + 60)
    y = round(box["y"])
    occupied = [(float(n["x"]), float(n["y"]), float(n["width"]), float(n["height"])) for n in data["nodes"]]
    for _ in range(64):
        clash = any(
            x < ox + ow + 20 and ox < x + width + 20 and y < oy + oh + 20 and oy < y + height + 20
            for ox, oy, ow, oh in occupied
        )
        if not clash:
            return (x, y)
        y += 90
        if y > box["y"] + box["height"] + 200:
            x += width + 80
            y = round(box["y"])
    return (x, y)


def apply(lib: notelib.Library, rel: str, ops: list[dict[str, Any]]) -> dict[str, Any]:
    """一批改动，校验完**一起落盘**（一次快照）。

    为什么不是"每个动作一个接口"：拖一张卡片前端会攒几十次位移，逐个落盘会把快照轮转
    冲干净（`MAX_SNAPSHOTS` 就那么几版），"撤销"随即失效。收一批就只有一个版本。
    """
    if not isinstance(ops, list) or not ops:
        raise CanvasError("没有要做的改动")
    current = read(lib, rel)
    data: dict[str, Any] = {"nodes": [dict(n) for n in current["nodes"]], "edges": [dict(e) for e in current["edges"]]}
    touched = 0

    for op in ops:
        if not isinstance(op, dict):
            raise CanvasError("改动项必须是对象")
        name = str(op.get("op") or "").strip()
        if name == "add_node":
            _add_node(data, op)
        elif name == "update_node":
            _update_node(data, op)
        elif name == "delete_node":
            _delete_node(data, op)
        elif name == "add_edge":
            _add_edge(data, op)
        elif name == "delete_edge":
            _delete_edge(data, op)
        else:
            raise CanvasError(f"不认识的改动：{name}")
        touched += 1

    if not touched:
        raise CanvasError("没有要做的改动")
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    notelib.save_raw(lib, rel, text, why="画布编辑")
    fresh = read(lib, rel)
    return {"items": ops, "canvas": fresh, "bbox": bbox(fresh), "count": touched}


def _add_node(data: dict[str, Any], op: dict[str, Any]) -> None:
    kind = str(op.get("type") or "text")
    if kind not in DEFAULT_SIZE:
        raise CanvasError(f"不认识的卡片类型：{kind}")
    width, height = DEFAULT_SIZE[kind]
    node: dict[str, Any] = {
        "id": new_id(),
        "type": kind,
        "width": max(MIN_SIZE[0], _num(op.get("width"), width)),
        "height": max(MIN_SIZE[1], _num(op.get("height"), height)),
    }
    if op.get("x") is None or op.get("y") is None:
        node["x"], node["y"] = free_slot(data, node["width"], node["height"])
    else:
        node["x"], node["y"] = _num(op.get("x"), 0), _num(op.get("y"), 0)
    if kind == "group":
        node["label"] = str(op.get("label") or "分组")
    elif kind == "file":
        target = str(op.get("file") or "").strip()
        if not target:
            raise CanvasError("文件卡片得给一个文件")
        node["file"] = target
    else:
        node["text"] = str(op.get("text") or "")
    if op.get("color") not in (None, ""):
        node["color"] = str(op["color"])
    data["nodes"].append(node)


def _find(data: dict[str, Any], node_id: str) -> dict[str, Any]:
    for node in data["nodes"]:
        if node["id"] == node_id:
            return node
    raise CanvasError(f"画布上没有这张卡片：{node_id}")


def _update_node(data: dict[str, Any], op: dict[str, Any]) -> None:
    node = _find(data, str(op.get("id") or ""))
    for key in ("x", "y"):
        if op.get(key) is not None:
            node[key] = _num(op.get(key), node[key])
    if op.get("width") is not None:
        node["width"] = max(MIN_SIZE[0], _num(op.get("width"), node["width"]))
    if op.get("height") is not None:
        node["height"] = max(MIN_SIZE[1], _num(op.get("height"), node["height"]))
    for key in ("text", "label", "file", "color"):
        if op.get(key) is not None:
            value = str(op[key])
            if value:
                node[key] = value
            else:
                node.pop(key, None)          # 传空串 = 清掉这个字段，不是"留一个空字符串"
    if not node.get("text") and not node.get("file") and not node.get("label"):
        node.setdefault("text" if node["type"] != "file" else "file", "")


def _delete_node(data: dict[str, Any], op: dict[str, Any]) -> None:
    node_id = str(op.get("id") or "")
    _find(data, node_id)
    data["nodes"] = [node for node in data["nodes"] if node["id"] != node_id]
    # 挂在它身上的边一并删掉：留着就是指向空气的边，渲染时必然炸
    data["edges"] = [edge for edge in data["edges"] if edge["fromNode"] != node_id and edge["toNode"] != node_id]


def _add_edge(data: dict[str, Any], op: dict[str, Any]) -> None:
    source = str(op.get("fromNode") or op.get("from") or "")
    target = str(op.get("toNode") or op.get("to") or "")
    if source == target:
        raise CanvasError("不能连到自己")
    _find(data, source)
    _find(data, target)
    if any(edge["fromNode"] == source and edge["toNode"] == target for edge in data["edges"]):
        return                               # 已经有这条边了：当成功，别报错（人也常连两次）
    edge: dict[str, Any] = {"id": new_id(), "fromNode": source, "toNode": target}
    for key in ("fromSide", "toSide"):
        if op.get(key) in SIDES:
            edge[key] = op[key]
    if op.get("label"):
        edge["label"] = str(op["label"])[:40]
    if op.get("color") not in (None, ""):
        edge["color"] = str(op["color"])
    data["edges"].append(edge)


def _delete_edge(data: dict[str, Any], op: dict[str, Any]) -> None:
    edge_id = str(op.get("id") or "")
    if not any(edge["id"] == edge_id for edge in data["edges"]):
        raise CanvasError(f"画布上没有这条连线：{edge_id}")
    data["edges"] = [edge for edge in data["edges"] if edge["id"] != edge_id]


__all__ = [
    "CanvasError",
    "DEFAULT_SIZE",
    "MIN_SIZE",
    "SIDES",
    "apply",
    "bbox",
    "free_slot",
    "new_id",
    "normalize",
    "read",
]
