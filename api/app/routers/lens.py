"""滤镜（lens）的增删改 —— 药丸里那份清单的写入口。

为什么要接口：滤镜**是用户自己的东西**（`app/lens.py` 的说明），
不是随包发的资源。既然 `ls <数据目录>/lens` 能看到，界面上就该能加、能改、能删 ——
"只能去文件夹里手写"对一个给普通人用的应用是不成立的。

三条路对应三个动作（界面里就是列表行的铅笔 / 垃圾桶 + 底部那个"新建"）：

* `GET    /api/lens`        清单（药丸里那份；`/api/chat/mounts` 也带一份，两处同源）
* `PUT    /api/lens/{key}`  新建或整条替换（写 md 文件，见 `lens.save`）
* `DELETE /api/lens/{key}`  删掉（只删文件；链上那些挂载记录留着，引用不存在的 key 会被忽略）

**不做的**：改 key（它同时是文件名，改 key = 换一条）；批量导入导出（那是题库包那套的事）。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from .. import lens as lens_lib
from ..deps import DbSession
from ..models import Conversation, LensRecord, Message

router = APIRouter(prefix="/api/lens", tags=["lens"])


@router.get("")
def list_lenses(db: DbSession) -> dict:
    """药丸里那份清单。**不带正文**（提示词是服务端的实现细节，界面只要名字与说明）。"""
    return {"items": lens_lib.catalog(db)}


def _conversation(db: DbSession, raw: object) -> Conversation:
    try:
        conv_id = uuid.UUID(str(raw or ""))
    except (ValueError, AttributeError) as exc:
        raise HTTPException(status_code=400, detail="cid 不是对话编号") from exc
    conv = db.get(Conversation, conv_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="没有这条对话")
    return conv


# **必须排在 `/{key}` 之前**：否则 `/records` 会被当成一条滤镜的 key。
@router.get("/records")
def list_records(db: DbSession, cid: str = "") -> dict:
    """挂载记录（`cid` 空 = 全部）。形状与前端 `QF.store` 那份一致。"""
    conv = _conversation(db, cid) if cid else None
    return {"items": lens_lib.records(db, str(conv.id) if conv else "")}


@router.put("/records")
def save_record(body: dict, db: DbSession) -> dict:
    """装 / 取一条滤镜。同一（对话、消息、key）只留一行。"""
    conv = _conversation(db, body.get("cid"))
    key = str(body.get("key") or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="key 不能空")
    mid = str(body.get("mid") or "").strip()
    message_uid = ""
    if mid:
        try:
            msg = db.get(Message, int(mid))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="mid 不是消息编号") from exc
        if msg is None or str(msg.conversation_id) != str(conv.id):
            raise HTTPException(status_code=400, detail="这条消息不在这个对话里")
        message_uid = str(msg.uid or "")
    one = db.scalar(
        select(LensRecord).where(
            LensRecord.conversation_id == conv.id,
            LensRecord.message_uid == message_uid,
            LensRecord.lens_key == key,
        )
    )
    on = body.get("on") is not False
    try:
        at = int(body.get("at") or 0)
    except (TypeError, ValueError):
        at = 0
    if one is None:
        one = LensRecord(
            conversation_id=conv.id,
            message_uid=message_uid,
            lens_key=key,
            enabled=on,
            at=at,
        )
        db.add(one)
    else:
        one.enabled = on
        one.at = at
    db.commit()
    return {
        "ok": True,
        "item": {"cid": str(conv.id), "mid": mid, "key": key, "on": on, "at": at},
    }


@router.get("/{key}")
def read_lens(key: str, db: DbSession) -> dict:
    """一条滤镜的**全文**（含提示词正文）—— 编辑时要把它填进输入框。

    清单（`GET /api/lens`）里刻意没有正文：那是服务端的实现细节，
    但"点开编辑"要看的正是正文，所以单开这一条。
    """
    one = lens_lib.find(key, db)
    if one is None:
        raise HTTPException(status_code=404, detail="没有这条滤镜")
    return {"item": {"key": one.key, "label": one.label, "kind": one.kind, "hint": one.hint, "prompt": one.prompt}}


@router.put("/{key}")
def save_lens(key: str, body: dict, db: DbSession) -> dict:
    """新建或整条替换一条。

    `key` 放在路径里而不是正文里：它是**文件名**，与"这条是什么"分开写，
    改错一个字段不至于把文件挪到别处。新建时也用这个口 —— 路径里的 key 自己起
    （界面里点"新建"给一个 `lens-<6hex>`，用户不用关心）。
    """
    try:
        one = lens_lib.save(
            key=key,
            label=str(body.get("label") or ""),
            kind=str(body.get("kind") or ""),
            hint=str(body.get("hint") or ""),
            prompt=str(body.get("prompt") or ""),
            db=db,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"item": {"key": one.key, "label": one.label, "kind": one.kind, "hint": one.hint}}


@router.post("")
def create_lens(body: dict, db: DbSession) -> dict:
    """新建（key 由服务端起）。界面里"新建"走这个，不用先想一个英文 key。"""
    try:
        one = lens_lib.save(
            label=str(body.get("label") or ""),
            kind=str(body.get("kind") or ""),
            hint=str(body.get("hint") or ""),
            prompt=str(body.get("prompt") or ""),
            db=db,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"item": {"key": one.key, "label": one.label, "kind": one.kind, "hint": one.hint}}


@router.delete("/{key}")
def delete_lens(key: str, db: DbSession) -> dict:
    if not lens_lib.delete(key, db):
        raise HTTPException(status_code=404, detail="没有这条滤镜")
    return {"ok": True}
