"""全局记忆：跨对话长期带着的、关于这个人的信息。

## 它与旁边几种「记住的东西」有什么不同

* **对话历史**只属于某一条对话；换一条线就看不见了。
* **笔记**是他自己写的、按主题归档的正文（文件在 vault 里）。
* **这里的记忆**是 AI 在对话里主动记下的、关于他的长期事实：偏好、背景、
  正在做的事。每条都短，**跨所有对话都成立** —— 这是"全局"的意思。

## 它不进系统提示词

2026-11-09 用户定的规矩：记忆**不自动注入**每一轮的提示词 ——
"放在那边，让 agent 有需要的时候看"（像 coding agent 的 global memory）。
所以它只是一张表和两个工具：`list_memory` 看、`edit_memory` 改。

代价与补偿：模型不会天然知道"他叫什么"。提示词里只留一句**使用指引**
（见 `routers/chat.py` 的 `GROUP_PROMPTS["memory"]`），让它在该查的时候查 ——
称呼、长期偏好、新话题开场、以及要给建议却拿不准上下文时。
极简模式没有工具，也就没有记忆；这与"极简只是没有工具"一致。
"""

from __future__ import annotations

from datetime import UTC
from typing import Any

from sqlalchemy import select

from .models import Memory

#: 记忆的分类。**闭集**：分类决定提示词里的分组，乱写的值会让分组散掉。
KINDS: tuple[str, ...] = ("profile", "preference", "project", "fact")

KIND_LABELS: dict[str, str] = {
    "profile": "身份 / 背景",
    "preference": "偏好 / 要求",
    "project": "在做的事",
    "fact": "记住的事实",
}

_MAX_TITLE = 200
_MAX_BODY = 20000


def kind_of(raw: Any) -> str:
    """分类归一化：不认识的值落到 `fact`（与排程的 `_focus` 同一条规矩）。"""
    text = str(raw or "").strip().lower()
    return text if text in KINDS else "fact"


def label_of(kind: str) -> str:
    return KIND_LABELS.get(kind, KIND_LABELS["fact"])


def clean_title(raw: Any, body: Any = "") -> str:
    """标题：给空就从正文第一行取一句 —— 模型偶尔会只给正文。"""
    title = str(raw or "").strip()[: _MAX_TITLE]
    if title:
        return title
    first = ""
    for line in str(body or "").splitlines():
        if line.strip():
            first = line.strip()
            break
    return first[:60] or "未命名记忆"


def clean_body(raw: Any) -> str:
    return str(raw or "")[:_MAX_BODY]


def _iso(value) -> str | None:  # noqa: ANN001
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z") if value else None


def as_json(row: Memory) -> dict:
    return {
        "id": row.id,
        "title": row.title,
        "body": row.body,
        "kind": row.kind,
        "kindLabel": label_of(row.kind),
        "createdAt": _iso(row.created_at),
        "updatedAt": _iso(row.updated_at),
    }


def _stamp(row: Memory) -> float:
    try:
        return float(row.updated_at.timestamp())
    except (AttributeError, ValueError):
        return 0.0


def entries(db) -> list[Memory]:  # noqa: ANN001
    """全部记忆，按「先身份/偏好、再项目、最后事实」排，同组内新的在前。"""
    rows = list(db.scalars(select(Memory)).all())
    order = {kind: index for index, kind in enumerate(KINDS)}
    rows.sort(key=lambda one: (order.get(one.kind, len(KINDS)), -_stamp(one)))
    return rows


def create(db, *, title: Any = "", body: Any = "", kind: Any = "fact") -> Memory:  # noqa: ANN001
    text = clean_body(body)
    row = Memory(title=clean_title(title, text), body=text, kind=kind_of(kind))
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def update(db, row: Memory, *, title: Any = None, body: Any = None, kind: Any = None) -> Memory:  # noqa: ANN001
    """只改传上来的字段（`None` = 没传）。"""
    if title is not None:
        row.title = clean_title(title, row.body if body is None else body)
    if body is not None:
        row.body = clean_body(body)
    if kind is not None:
        row.kind = kind_of(kind)
    db.commit()
    db.refresh(row)
    return row


def delete(db, row: Memory) -> None:  # noqa: ANN001
    db.delete(row)
    db.commit()
