"""全局记忆的读写面（设置 → 记忆）。

对齐的是「代理的记忆」而不是「聊天记录」：条目短、可改、可删，**不自动进提示词**
（用户 2026-11-09 定的：放在那边，有需要时自己看；见 `app/memory.py`）。这里只做
增删改查 —— 对话里的入口是 `list_memory` / `edit_memory` 工具，两边共用
`app/memory.py` 的同一套函数。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .. import memory
from ..deps import DbSession
from ..models import Memory

router = APIRouter(prefix="/api", tags=["memory"])


@router.get("/memory")
def list_memories(db: DbSession) -> dict:
    return {"memories": [memory.as_json(one) for one in memory.entries(db)]}


@router.post("/memory")
def create_memory(body: dict, db: DbSession) -> dict:
    title = str(body.get("title") or "").strip()
    text = str(body.get("body") or "").strip()
    if not title and not text:
        raise HTTPException(status_code=400, detail="记一条空的没有意义：标题和正文至少要有一个")
    row = memory.create(
        db,
        title=body.get("title"),
        body=body.get("body"),
        kind=body.get("kind"),
    )
    return {"memory": memory.as_json(row)}


@router.patch("/memory/{memory_id}")
def update_memory(memory_id: str, body: dict, db: DbSession) -> dict:
    row = db.get(Memory, memory_id)
    if row is None:
        raise HTTPException(status_code=404, detail="没有这条记忆")
    memory.update(
        db,
        row,
        title=body.get("title"),
        body=body.get("body"),
        kind=body.get("kind"),
    )
    return {"memory": memory.as_json(row)}


@router.delete("/memory/{memory_id}")
def delete_memory(memory_id: str, db: DbSession) -> dict:
    row = db.get(Memory, memory_id)
    if row is None:
        raise HTTPException(status_code=404, detail="没有这条记忆")
    memory.delete(db, row)
    return {"ok": True}
