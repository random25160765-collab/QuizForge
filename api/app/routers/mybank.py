"""用户题单 —— 模型或用户**自己出**的题。

与 `/api/bank`（公共题库）分开是有意的：那一侧是只读的公共资产（覆盖率、图谱、
出题流水线都挂在上面），这一侧归用户所有，可增、可改、可删。

`payload` 与公共题的 payload **同构**，所以渲染与判分的代码不分叉；
练习/组卷两端都能取题，靠 `scope` 区分来源（见 `knowledge.py` 的 `/picks`）。

"随出随改"落在两点上：
* 对话里出的题**先只留在对话里**（前端的一个零件），用户按「存进题单」才落库；
* 存进来之后还能改（`PATCH`）—— 一道题常常要改两三遍才顺眼。
"""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from ..deps import AuthenticatedWriter, CurrentUser, DbSession
from ..models import UserQuestion

router = APIRouter(prefix="/api/my", tags=["mybank"])

# 题单的上限：它是"自己攒的题"，不是第二座题库。
# 到了上限就该去整理（删旧的、或把好的并进公共题库），而不是继续堆。
MAX_USER_QUESTIONS = 500
MAX_PAYLOAD_CHARS = 40_000

QUESTION_TYPES = ("single", "multi", "blank", "short", "problem")


def _out(row: UserQuestion) -> dict:
    return {
        "id": row.id,
        "payload": row.payload or {},
        "pointKey": row.point_key or "",
        "conversationId": str(row.conversation_id) if row.conversation_id else "",
        "source": "mine",
        "createdAt": row.created_at.isoformat() if row.created_at else None,
        "updatedAt": row.updated_at.isoformat() if row.updated_at else None,
    }


def _clean(body: dict) -> dict:
    """校验并规整一份用户题。

    刻意**不**做公共题库那套严格校验（那是出题流水线的活）：用户自己的题，
    他自己说了算。这里只挡住"根本不成题"的（没题干、类型不在表里）与超大 payload。
    """
    question = body.get("payload")
    if not isinstance(question, dict):
        raise HTTPException(400, "要给出 payload（题目本身）。")

    stem = str(question.get("stem") or "").strip()
    if not stem:
        raise HTTPException(400, "题干不能为空。")

    qtype = str(question.get("type") or "single").strip().lower()
    if qtype not in QUESTION_TYPES:
        qtype = "single"

    cleaned = {key: value for key, value in question.items() if key not in ("id", "file", "source")}
    cleaned["stem"] = stem
    cleaned["type"] = qtype

    if len(json.dumps(cleaned, ensure_ascii=False)) > MAX_PAYLOAD_CHARS:
        raise HTTPException(413, "这道题太大了，精简一下再存。")
    return cleaned


@router.get("/questions")
def list_my_questions(user: CurrentUser, db: DbSession) -> dict:
    """我的题单（新的在前）。"""
    rows = db.scalars(
        select(UserQuestion)
        .where(UserQuestion.user_id == user.id)
        .order_by(UserQuestion.created_at.desc())
        .limit(MAX_USER_QUESTIONS)
    ).all()
    return {"questions": [_out(row) for row in rows], "count": len(rows)}


@router.post("/questions")
def create_my_question(payload: dict, user: AuthenticatedWriter, db: DbSession) -> dict:
    """把一道题收进我的题单。

    对话里出的题**先不落库**（那是"临时题"）—— 用户按「存进题单」才走到这里。
    所以这个接口的语义是"我决定留着它"，而不是"缓存一下"。
    """
    body = payload or {}
    count = len(
        db.scalars(select(UserQuestion.id).where(UserQuestion.user_id == user.id)).all()
    )
    if count >= MAX_USER_QUESTIONS:
        raise HTTPException(
            409, f"题单满了（{MAX_USER_QUESTIONS} 道）—— 先删几道，或者把好的并进公共题库。"
        )

    question = _clean(body)
    row = UserQuestion(
        id="uq-" + uuid.uuid4().hex[:12],
        user_id=user.id,
        payload=question,
        point_key=str(body.get("pointKey") or "").strip()[:96],
    )
    conversation_id = str(body.get("conversationId") or "").strip()
    if conversation_id:
        try:
            row.conversation_id = uuid.UUID(conversation_id)
        except ValueError:
            row.conversation_id = None
    db.add(row)
    db.commit()
    return {"question": _out(row)}


@router.patch("/questions/{question_id}")
def update_my_question(
    question_id: str, payload: dict, user: AuthenticatedWriter, db: DbSession
) -> dict:
    """改一道（随出随改：题目常常要改两三遍才顺眼）。"""
    row = db.scalars(
        select(UserQuestion).where(
            UserQuestion.id == question_id, UserQuestion.user_id == user.id
        )
    ).first()
    if row is None:
        raise HTTPException(404, "题单里没有这道题。")

    body = payload or {}
    if "payload" in body:
        row.payload = _clean(body)
    if "pointKey" in body:
        row.point_key = str(body.get("pointKey") or "").strip()[:96]
    db.commit()
    return {"question": _out(row)}


@router.delete("/questions/{question_id}")
def delete_my_question(question_id: str, user: AuthenticatedWriter, db: DbSession) -> dict:
    """删一道。

    公共题库那边只下架不删除（它要能追溯）；用户题单**必须能删** ——
    这是"这是我自己攒的东西"的一部分。
    """
    row = db.scalars(
        select(UserQuestion).where(
            UserQuestion.id == question_id, UserQuestion.user_id == user.id
        )
    ).first()
    if row is None:
        raise HTTPException(404, "题单里没有这道题。")
    db.delete(row)
    db.commit()
    return {"deleted": question_id}


__all__ = ["router", "MAX_USER_QUESTIONS", "QUESTION_TYPES"]
