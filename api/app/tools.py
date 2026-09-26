"""给对话用的工具表 —— 模型能"伸手"去拿的那几样东西。

## 为什么先只做只读的

写操作（改掌握度、推题入队）会**改变用户的状态**，它需要"先确认再执行"那一层
（LibreChat 的 approval 就是干这个的），而那一层还没做。所以这一版只放**只读**工具：
它们最多花一点 token，不会把谁的进度改坏。写操作与题卡一起放下一刀。

## 工具描述就是契约

描述不是给人看的说明，是**模型判断"什么时候该用它"的唯一依据**。
所以每条都写清：什么时候用、参数是什么、返回什么形状。
中文描述是刻意的 —— 这个项目面向的是中文语料与国内模型。

## 异常边界

工具内部抛的任何异常都被 `call()` 收成 `{"error": ...}` 交回模型，
**不让它中断这一轮**：模型看到"这次查询失败了"，可以换个问法再来；
而如果直接抛到路由，用户得到的是一次白屏，且额度已经花了。

## 一处刻意的取舍

`get_existing_questions` 默认**不给答案**（要 `includeAnswer` 才给）。
因为它的下一个用途是"推一道题给学生做"，把答案放进上下文等于剧透；
要让模型讲题时，它自己会传 includeAnswer。
"""

from __future__ import annotations

import html
import json
import re
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.orm.attributes import flag_modified

from . import heavy_deps, mastery, materials, recursion, semantic
from .models import (
    Concept,
    ConceptEdge,
    Conversation,
    KnowledgePoint,
    Material,
    Message,
    PointSource,
    Question,
    QuestionPoint,
    Record,
)


def _clamp(value, low: int, high: int, default: int) -> int:  # noqa: ANN001
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(n, high))


def _stem(question: Question, limit: int = 120) -> str:
    payload = question.payload or {}
    text = str(payload.get("stem") or payload.get("question") or "")
    text = " ".join(text.split())
    return text[:limit] + ("…" if len(text) > limit else "")


# ---------------------------------------------------------------- 检索


def search_knowledge(db, args, ctx=None) -> dict:  # noqa: ANN001
    query = str(args.get("query") or "").strip()
    limit = _clamp(args.get("limit"), 1, 20, 8)
    if not query:
        return {"error": "query 不能为空"}

    like = "%" + query + "%"
    concepts = db.execute(
        select(
            Concept.key,
            Concept.name,
            Concept.kind,
            Concept.definition,
            Concept.material_count,
            Concept.question_count,
            Concept.topic_key,
        )
        .where(Concept.status != "retired")
        .where(
            or_(
                Concept.key.ilike(like),
                Concept.name.ilike(like),
                Concept.definition.ilike(like),
                cast(Concept.aliases, String).ilike(like),
            )
        )
        .order_by(Concept.material_count.desc(), Concept.question_count.desc(), Concept.key)
        .limit(limit)
    ).all()

    points = db.execute(
        select(
            KnowledgePoint.key,
            KnowledgePoint.name,
            KnowledgePoint.kind,
            KnowledgePoint.layers,
            KnowledgePoint.thickness,
            Material.slug,
            Material.title,
        )
        .join(Material, Material.id == KnowledgePoint.material_id)
        .where(or_(KnowledgePoint.key.ilike(like), KnowledgePoint.name.ilike(like)))
        .order_by(KnowledgePoint.key)
        .limit(limit)
    ).all()

    return {
        "concepts": [
            {
                "key": key,
                "name": name,
                "kind": kind,
                "definition": " ".join((definition or "").split())[:200],
                "materials": material_count,
                "questions": question_count,
                "topic": topic_key,
            }
            for key, name, kind, definition, material_count, question_count, topic_key in concepts
        ],
        "points": [
            {
                "key": key,
                "name": name,
                "kind": kind,
                "layers": layers or [],
                "thickness": thickness,
                "material": slug,
                "title": title,
            }
            for key, name, kind, layers, thickness, slug, title in points
        ],
        "note": "concepts 是跨材料的那个「东西」，points 是它在某份材料里的一次出现；"
        "要看原文片段与挂着的题，用 get_point_detail(key)。",
    }


def get_point_detail(db, args, ctx=None) -> dict:  # noqa: ANN001
    key = str(args.get("key") or "").strip()
    if not key:
        return {"error": "key 不能为空"}

    point = db.scalar(
        select(KnowledgePoint).where(KnowledgePoint.key == key).order_by(KnowledgePoint.id).limit(1)
    )
    if point is not None:
        return _point_detail(db, point)

    concept = db.scalar(select(Concept).where(Concept.key == key))
    if concept is not None:
        return _concept_detail(db, concept)

    # 退一步：既不是点也不是概念 key 时，用模糊搜给个线索，而不是干巴巴一句"没有"
    preview = search_knowledge(db, {"query": key, "limit": 5})
    return {"error": f"没有 key 为 {key} 的点或概念", "similar": preview}


def _sources(db, point: KnowledgePoint) -> list[dict]:  # noqa: ANN001
    rows = db.execute(
        select(Material.slug, Material.title, PointSource.start_line, PointSource.end_line)
        .select_from(PointSource)
        .join(KnowledgePoint, KnowledgePoint.id == PointSource.point_id)
        .join(Material, Material.id == KnowledgePoint.material_id)
        .where(PointSource.point_id == point.id)
        .order_by(PointSource.start_line)
        .limit(12)
    ).all()
    return [
        {"material": slug, "title": title, "startLine": start, "endLine": end}
        for slug, title, start, end in rows
    ]


def _questions_of(  # noqa: ANN001
    db, point_keys: list[str], limit: int, include_answer: bool, layer: str = "", qtype: str = ""
) -> list[dict]:
    if not point_keys:
        return []
    stmt = (
        select(Question, KnowledgePoint.key)
        .join(QuestionPoint, QuestionPoint.question_id == Question.id)
        .join(KnowledgePoint, KnowledgePoint.id == QuestionPoint.point_id)
        .where(KnowledgePoint.key.in_(point_keys))
        .where(Question.retired_at.is_(None))
    )
    # 层必须**在 limit 之前**筛。原先是在结果上过滤 —— 先按 id 取前 N 条再筛，
    # 于是"应用层有 104 道填空 + 34 道大题"这件事被截成了 2 条填空，
    # 模型据此得出"题库里没有 problem"（错的，而且错得理直气壮）。
    if layer:
        stmt = stmt.where(Question.layer == layer)
    # 题型同理，**同样在 limit 之前**。这一条是同一个坑的第二次：两条通道原先
    # 都没暴露题型，而候选是按 id 排序取的 —— single 的 id 更小，永远排在
    # problem 前面，于是"34 道大题一道都推不到、也读不到"（2026-09-20 实测）。
    if qtype:
        stmt = stmt.where(Question.type == qtype)
    rows = db.execute(stmt.order_by(Question.id).limit(limit)).all()
    out = []
    for question, point_key in rows:
        item = {
            "id": question.id,
            "point": point_key,
            "type": question.type,
            "layer": question.layer,
            "wing": question.wing,
            "difficulty": question.difficulty,
            # 题干：**讲解场景**（includeAnswer）给完整的 —— 模型是照着题干讲的，
            # 截到 120 字等于让它讲一半（用户实测："拿不到完整题干"）。
            # 平时仍截短，省上下文。
            "stem": _stem(question, 2000 if include_answer else 120),
        }
        if include_answer:
            payload = question.payload or {}
            item["answer"] = str(payload.get("answer") or payload.get("answers") or "")[:300]
            # 大题没有 `answer`（答案是每问的 reference），给个不空的：
            subs = payload.get("parts") or payload.get("questions") or []
            if not item["answer"] and isinstance(subs, list) and subs:
                item["answer"] = "；".join(
                    str(sub.get("reference") or "").strip()
                    for sub in subs
                    if isinstance(sub, dict) and str(sub.get("reference") or "").strip()
                )[:600]
        out.append(item)
    return out


def _point_detail(db, point: KnowledgePoint) -> dict:  # noqa: ANN001
    material = db.get(Material, point.material_id)
    return {
        "kind_of_node": "point",
        "key": point.key,
        "name": point.name,
        "type": point.kind,
        "layers": point.layers or [],
        "thickness": point.thickness,
        "producible": point.producible,
        "material": {
            "slug": material.slug if material else "",
            "title": material.title if material else "",
            "subject": material.subject if material else "",
        },
        "sources": _sources(db, point),
        "concept": _concept_ref(db, point.concept_id),
        "questions": _questions_of(db, [point.key], 8, False),
        "note": point.note or "",
    }


def _concept_ref(db, concept_id) -> dict | None:  # noqa: ANN001
    if not concept_id:
        return None
    concept = db.get(Concept, concept_id)
    if concept is None:
        return None
    return {"key": concept.key, "name": concept.name, "materials": concept.material_count}


def _concept_detail(db, concept: Concept) -> dict:  # noqa: ANN001
    points = db.execute(
        select(KnowledgePoint, Material.slug)
        .join(Material, Material.id == KnowledgePoint.material_id)
        .where(KnowledgePoint.concept_id == concept.id)
        .order_by(Material.slug)
        .limit(20)
    ).all()

    edges = db.execute(
        select(ConceptEdge.type, ConceptEdge.derived_by, Concept.name)
        .join(Concept, Concept.id == ConceptEdge.to_concept_id)
        .where(ConceptEdge.from_concept_id == concept.id)
        .order_by(ConceptEdge.type)
        .limit(20)
    ).all()

    return {
        "kind_of_node": "concept",
        "key": concept.key,
        "name": concept.name,
        "type": concept.kind,
        "definition": concept.definition,
        "aliases": concept.aliases or [],
        "topic": concept.topic_key,
        "counts": {
            "materials": concept.material_count,
            "points": concept.point_count,
            "questions": concept.question_count,
        },
        "appearances": [
            {
                "key": point.key,
                "name": point.name,
                "layers": point.layers or [],
                "material": slug,
                "sources": _sources(db, point),
            }
            for point, slug in points
        ],
        "edges": [
            {"type": type_, "to": name, "derivedBy": derived_by}
            for type_, derived_by, name in edges
        ],
        "questions": _questions_of(db, [point.key for point, _ in points], 8, False),
    }


# ---------------------------------------------------------------- 状态


def get_existing_questions(db, args, ctx=None) -> dict:  # noqa: ANN001
    point_key = str(args.get("pointKey") or "").strip()
    layer = str(args.get("layer") or "").strip()
    qtype = str(args.get("type") or "").strip()
    include_answer = bool(args.get("includeAnswer"))
    limit = _clamp(args.get("limit"), 1, 20, 5)

    keys = [point_key] if point_key else []
    if not keys:
        rows = db.execute(
            select(KnowledgePoint.key)
            .join(QuestionPoint, QuestionPoint.point_id == KnowledgePoint.id)
            .join(Question, Question.id == QuestionPoint.question_id)
            .where(Question.retired_at.is_(None))
            .group_by(KnowledgePoint.key)
            .order_by(KnowledgePoint.key)
            .limit(60)
        ).all()
        keys = [row[0] for row in rows]

    items = _questions_of(db, keys, limit, include_answer, layer=layer, qtype=qtype)
    return {
        "items": items,
        # 顺带把**题库的形状**报出来：类型 × 层 各有多少。
        # 不加这一项，模型就会拿到的样本去推整体（实测推错成"没有大题"）——
        # 这是可以让数据库直接回答的问题，不该让它猜。
        "bank": _bank_shape(db),
        "note": "answer 默认不给（推题时剧透）；要讲解就把 includeAnswer 设为 true。"
        "items 是**抽样**，要判「题库里有没有某类题」请看 bank（那是全量统计）。"
        "指定 `type`（如 problem）可以只取某一类 —— 不指定时按 id 排，前面的可能总是同一类。",
    }


def _bank_shape(db) -> dict:  # noqa: ANN001
    """题库的形状：{type: {layer: 数量}}，只数已发布的。"""
    rows = db.execute(
        select(Question.type, Question.layer, func.count())
        .where(Question.retired_at.is_(None), Question.status == "published")
        .group_by(Question.type, Question.layer)
    ).all()
    shape: dict[str, dict[str, int]] = {}
    for qtype, layer, count in rows:
        shape.setdefault(str(qtype or "?"), {})[str(layer or "（未标层）")] = int(count)
    return shape


def _record_dict(record: Record | None) -> dict:  # noqa: ANN001
    """库里的记录 → `mastery.from_record` 认的形状（camelCase）。

    注意 `streak` 在 `patch` 里而不是独立列 —— 它是前端算的，随同步一起上来。
    """
    if record is None:
        return {}
    patch = record.patch or {}
    return {
        "attempts": record.attempts,
        "correct": record.correct,
        "streak": patch.get("streak") or 0,
        "lastAt": record.last_at,
    }


def _bands_by_point(db, keys=None):  # noqa: ANN001
    """按知识点算掌握档位 —— `get_mastery` 与图检索**共用**这一份口径。

    为什么必须共用：图检索里"这个前置还没打牢"的判据，与 `get_mastery`
    报出来的那个数字，必须是同一个 —— 否则同一件事在两个工具里给出两种说法，
    而用户只能信一个（他会信那个对他有利的）。

    没答过的点照样返回（`band: new`），因为"这片是空的"本身就是要说的事。
    """
    now = int(time.time() * 1000)
    records = {
        record.question_id: record
        for record in db.scalars(select(Record)).all()
    }
    rows = db.execute(
        select(QuestionPoint.question_id, KnowledgePoint.key)
        .join(KnowledgePoint, KnowledgePoint.id == QuestionPoint.point_id)
    ).all()

    buckets: dict[str, list[tuple[int, str, int]]] = {}
    for question_id, key in rows:
        if keys and key not in keys:
            continue
        record = records.get(question_id)
        score, band = mastery.from_record(_record_dict(record), now)
        attempts = int(record.attempts) if record is not None else 0
        buckets.setdefault(key, []).append((score, band, attempts))

    out: dict[str, dict] = {}
    for key, bucket in buckets.items():
        scored = [item for item in bucket if item[2] > 0]
        if not scored:
            out[key] = {"key": key, "band": "new", "score": 0, "answered": 0, "attempts": 0}
            continue
        total_attempts = sum(item[2] for item in scored)
        avg = int(round(sum(item[0] for item in scored) / len(scored)))
        out[key] = {
            "key": key,
            "band": mastery.mastery_band(avg, total_attempts),
            "score": avg,
            "answered": len(scored),
            "attempts": total_attempts,
        }
    return out


def get_mastery(db, args, ctx=None) -> dict:  # noqa: ANN001
    keys = [str(k) for k in (args.get("pointKeys") or []) if str(k).strip()]
    bands = _bands_by_point(db, keys or None)
    items = sorted(bands.values(), key=lambda item: (item["score"], item["key"]))
    return {
        "items": items[:40],
        "note": "score 是该点下各题掌握度的平均（0–100）；band 是 new/learning/familiar/mastered 档位。",
    }


def get_due_reviews(db, args, ctx=None) -> dict:  # noqa: ANN001
    limit = _clamp(args.get("limit"), 1, 30, 10)
    now = int(time.time() * 1000)

    rows = db.execute(
        select(Record.question_id, Record.patch, Question.layer, Question.type)
        .join(Question, Question.id == Record.question_id)
        .where(Question.retired_at.is_(None))
    ).all()

    due = []
    for question_id, patch, layer, type_ in rows:
        sm2 = (patch or {}).get("sm2") or {}
        at = sm2.get("due")
        if isinstance(at, (int, float)) and at <= now:
            due.append((int(at), question_id, layer, type_))
    due.sort()

    keys = dict(
        db.execute(
            select(QuestionPoint.question_id, KnowledgePoint.key)
            .join(KnowledgePoint, KnowledgePoint.id == QuestionPoint.point_id)
        ).all()
    )
    return {
        "items": [
            {
                "questionId": question_id,
                "layer": layer,
                "type": type_,
                "pointKeys": [keys[question_id]] if question_id in keys else [],
                "overdueDays": round((now - at) / 86400000, 1),
            }
            for at, question_id, layer, type_ in due[:limit]
        ],
        "dueTotal": len(due),
        "note": "只算还在题库里的题；due 来自记录里的 SM2 状态。",
    }


# ---------------------------------------------------------------- 题卡


# 能推成卡片的题型。**大题（problem）也在其中**：卡片按"每问一个输入区"渲染
#（见前端 `cardInput` 的 problem 分支），每问的参考答案与评分要点不进卡
#（见 `_card_payload`）。
#
# 原先这里把它排除在外，理由是"那是另一套界面"—— 现在不这么分了：题库里的大题
# 与临时大题走**同一条路**（推成一张卡、在本页作答、答完交给本页的模型批改），
# 那条"独立子窗口 + 专职子代理"的路因此退役
#（用户："目前题库里的大题是单独界面单独子代理——这个改成和临时大题一样的配置"）。
CARD_TYPES = ("single", "multi", "blank", "short", "problem")


def _card_payload(question: Question) -> dict:
    """把一道题削成一张**不含答案**的卡。

    前端判分不靠这张卡 —— 它用本地题库（`QF.data.get(id)`）里的完整题面判，
    所以这里一个答案字段都不带。理由是硬的：卡片会进模型上下文、也会被渲染出来，
    答案摆在眼前就等于没题可做了（`answer` / `explanation` / `rubric` 全部剥掉）。
    """
    payload = question.payload or {}
    card = {
        "questionId": question.id,
        "type": question.type,
        "layer": question.layer,
        "wing": question.wing,
        "difficulty": question.difficulty,
        "topic": question.topic,
        "chapter": question.chapter,
        "stem": str(payload.get("stem") or ""),
    }

    options = payload.get("options")
    if isinstance(options, list):
        card["options"] = [
            {"key": str(item.get("key") or ""), "text": str(item.get("text") or "")}
            for item in options
            if isinstance(item, dict)
        ]

    hint = str(payload.get("hint") or "").strip()
    if hint:
        card["hint"] = hint  # 提示是给人看的，不是答案

    if question.type == "blank":
        # 填空只给"几个空"与作答方式，每个空的接受答案不给
        card["blankMode"] = str(payload.get("blankMode") or "")
        card["blankCount"] = len(payload.get("answer") or [])

    if question.type == "problem":
        # 大题：只带**小问的题面**（题号 / 标题 / 那一问在问什么）。
        # 每问的 `reference`（参考答案）与 `rubric`（评分要点）**必须剥掉** ——
        # 卡片会进模型上下文、也会被渲染出来，留在卡里就等于把答案摆在眼前。
        # 要对答案就点提交（前端本地题库里有完整题面与答案）；要讨论就让模型
        # 自己去 `get_existing_questions(includeAnswer)` 取。
        subs = payload.get("parts") or payload.get("questions") or []
        card["questions"] = [
            {
                "index": int(sub.get("index") or position + 1),
                "title": str(sub.get("title") or ""),
                "stem": str(sub.get("stem") or ""),
            }
            for position, sub in enumerate(subs)
            if isinstance(sub, dict)
        ]

    return card


def _pushed_ids(db, ctx) -> set[str]:  # noqa: ANN001
    """这次对话里已经推过的题。

    模型自己看不见（卡片是零件，按设计不回放进上下文），所以"再来一道"很容易
    变成同一道 —— 这个排除由服务端做，因为服务端看得见消息里的卡片。
    """
    conv_id = (ctx or {}).get("conversationId")
    if not conv_id:
        return set()
    rows = db.scalars(
        select(Message.parts).where(Message.conversation_id == uuid.UUID(str(conv_id)))
    ).all()
    out: set[str] = set()
    for parts in rows:
        for part in parts or []:
            if isinstance(part, dict) and part.get("type") == "card":
                question_id = (part.get("payload") or {}).get("questionId")
                if question_id:
                    out.add(str(question_id))
    return out


def push_question(db, args, ctx=None) -> dict:  # noqa: ANN001
    """推一道题给学生做：返回一张题卡（不含答案）。

    ## 选题规则刻意写得笨

    它必须**可解释、可复现**（与 `/api/picks` 同一条规矩）：同样的输入给同样的结果，
    否则"为什么给我这道题"永远说不清。所以是：筛选（知识点 / 层 / 翼 / 难度上限）
    → 排掉已经答对的（推一道他会了的题没有意义）→ 排掉这次已经推过的
    → 优先没做过的 → 同分按题目 id。

    ## 为什么答案不在这里给

    见 `_card_payload`。要讲评时模型自己会调 `get_existing_questions(includeAnswer)`，
    那是它主动要来看的，不是被塞进上下文的。
    """
    point_key = str(args.get("pointKey") or "").strip()
    layer = str(args.get("layer") or "").strip()
    wing = str(args.get("wing") or "").strip()
    qtype = str(args.get("type") or "").strip()
    if qtype and qtype not in CARD_TYPES:
        return {"card": None, "note": "题型只有这些：" + " / ".join(CARD_TYPES)}

    stmt = select(Question).where(Question.retired_at.is_(None), Question.type.in_(CARD_TYPES))
    if qtype:
        # **点名要大题就给大题**。原先这条通道没有题型参数，而下面按 id 排序取 ——
        # single 的 id 更小，永远排在前面，34 道 problem 一道都推不到
        #（2026-09-20 实测：给了 pointKey，那个点上确实同时挂着 problem，仍然推 single）。
        stmt = stmt.where(Question.type == qtype)
    if layer:
        stmt = stmt.where(Question.layer == layer)
    if wing:
        stmt = stmt.where(Question.wing == wing)
    if args.get("maxDifficulty"):
        stmt = stmt.where(Question.difficulty <= _clamp(args.get("maxDifficulty"), 1, 5, 5))
    if point_key:
        stmt = (
            stmt.join(QuestionPoint, QuestionPoint.question_id == Question.id)
            .join(KnowledgePoint, KnowledgePoint.id == QuestionPoint.point_id)
            .where(KnowledgePoint.key == point_key)
        )

    rows = db.scalars(stmt.order_by(Question.id).limit(400)).all()
    if not rows:
        return {"card": None, "note": "这个范围内没有可推的题，放宽 layer / wing / 知识点再试。"}

    records = {
        record.question_id: record
        for record in db.scalars(select(Record)).all()
    }
    pushed = _pushed_ids(db, ctx)

    fresh: list[Question] = []
    missed: list[Question] = []
    repeated: list[Question] = []
    for question in rows:
        record = records.get(question.id)
        if record is not None and record.attempts and record.last_status == "correct":
            continue  # 答对了就是会了 —— 这一条**永远**不推，兜底也不能把它捞回来
        if str(question.id) in pushed:
            repeated.append(question)  # 这次对话里推过了，只在没得选时才复用
            continue
        (fresh if record is None or not record.attempts else missed).append(question)

    pool = fresh or missed or repeated
    if not pool:
        return {
            "card": None,
            "note": "这个范围内的题他都答对了 —— 换个知识点，或者把 layer / wing 放宽。",
        }

    card = _card_payload(pool[0])
    return {
        "card": card,
        "note": "卡片由界面渲染给他作答，答完结果会自动写进答题记录（与刷题页同一条路径）。"
        "你**不要**报答案、也不要替他念选项；等他答完再讲。",
    }


# ---------------------------------------------------------------- 跑 Python


# Pyodide：CPython 编译成 WASM，在浏览器里**真跑** Python（不是模拟、不是转译）。
#
# 运行时**不进安装包**：它 76M，而应用其余部分加起来约 5M。它缓存在
# `data/cache/pyodide/`，第一次打开时由 `heavy_deps.ensure()` 取一次（从仓库
# `vendor/` 拷、或按清单下载）并逐个校验 sha256，之后离线可用。
# 位置、下载、校验、失败处理都在 `app/heavy_deps.py` 里 —— 这里只管怎么用。
PYODIDE_VERSION = heavy_deps.PYODIDE_VERSION


def pyodide_base() -> str | None:
    """运行时从哪加载（`None` = 本机还没有）。

    `__ORIGIN__` 是给前端填的占位符：沙箱页面里**相对路径解析不了**
    （srcdoc 文档的 base 是 about:srcdoc，`new URL('/assets/…')` 直接抛
    "Invalid URL"），而沙箱里 `location.origin` 是不透明的、页面自己也拼不出来。
    只有宿主知道自己的 origin，所以由它替换。

    为什么不再退回 CDN：**沙箱不该依赖外网**（实测现下运行时会让面板
    "正在加载运行时"停 60 秒）。现在只有一条路 —— 本机缓存，而缓存由
    `ensure()` 负责填。
    """
    return heavy_deps.base_url()

# 代码上限：它会被嵌进零件的库、每次读会话都要发给前端（同 render_demo 的道理）
CODE_MAX_CHARS = 20_000

# Pyodide 页面的样板。**由服务端写死**，模型只交 Python ——
# 让模型每次手写一遍加载与 stdout 接管，迟早会有一次写错，
# 而写错的表现是"一片空白"，用户根本看不出哪里坏了。
_PYODIDE_PAGE = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>__TITLE__</title>
<style>
 / * 与 app.css 同一套令牌（沙箱里读不到父页面的 CSS，只能抄一份过来）。
    抄的是**值**不是规则：面板是独立文档，样式必须自足。 */
 @font-face { font-family: 'JetBrains Mono'; font-style: normal; font-weight: 400;
              font-display: swap;
              src: url('__ORIGIN__/assets/fonts/jetbrains-mono-latin-400-normal.woff2') format('woff2'); }
 @font-face { font-family: 'JetBrains Mono'; font-style: normal; font-weight: 700;
              font-display: swap;
              src: url('__ORIGIN__/assets/fonts/jetbrains-mono-latin-700-normal.woff2') format('woff2'); }
 :root {
   color-scheme: dark;
   --bg: #0e1116; --bg2: #161b22; --line: #38495a; --line-soft: #2a3240;
   --fg: #e8eef5; --fg2: #c3ccd8; --fg3: #75838f;
   --pri: #2dd4bf; --ok: #34d399; --bad: #f87171; --amber: #fbbf24;
   --mono: 'JetBrains Mono', ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, monospace;
 }
 * { box-sizing: border-box; }
 html, body { height: 100%; }
 body { margin: 0; background: var(--bg); color: var(--fg);
        font: 13px/1.65 var(--mono);
        display: flex; flex-direction: column; }

 .pane__head { display: flex; align-items: center; gap: 9px; flex: none;
               padding: 10px 14px; border-bottom: 1px solid var(--line-soft);
               position: sticky; top: 0; background: var(--bg2); z-index: 2; }
 .dot { flex: none; width: 8px; height: 8px; border-radius: 50%;
        background: var(--amber); box-shadow: 0 0 0 0 rgba(251, 191, 36, 0.5);
        animation: pulse 1.6s ease-out infinite; }
 .dot.is-ok { background: var(--ok); animation: none; }
 .dot.is-bad { background: var(--bad); animation: none; }
 @keyframes pulse {
   0% { box-shadow: 0 0 0 0 rgba(251, 191, 36, 0.45); }
   70% { box-shadow: 0 0 0 7px rgba(251, 191, 36, 0); }
   100% { box-shadow: 0 0 0 0 rgba(251, 191, 36, 0); }
 }
 .pane__title { flex: 1; min-width: 0; margin: 0; font-size: 13px; font-weight: 600;
                white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
 .pane__badge { flex: none; font-size: 11px; color: var(--fg3);
                border: 1px solid var(--line-soft); border-radius: 999px;
                padding: 1px 9px; max-width: 55%; overflow: hidden;
                text-overflow: ellipsis; white-space: nowrap; }

 .pane__out { flex: 1; margin: 0; padding: 13px 15px 18px;
              white-space: pre-wrap; overflow-wrap: anywhere; }
 .pane__out .ok { color: var(--ok); }
 .pane__out .bad { color: var(--bad); }
 .pane__out .meta { color: var(--fg3); }

 .pane__foot { flex: none; padding: 7px 14px; border-top: 1px solid var(--line-soft);
               background: var(--bg2); color: var(--fg3); font-size: 11.5px; }
</style></head>
<body>
<header class="pane__head">
  <span class="dot" id="dot"></span>
  <h1 class="pane__title">__TITLE__</h1>
  <span class="pane__badge" id="badge">正在启动…</span>
</header>
<pre class="pane__out" id="out"></pre>
<footer class="pane__foot" id="foot">正在加载 Python 运行时（本机 13MB，浏览器会缓存）…</footer>
<script src="__INDEX__pyodide.js"></script>
<script>
const out = document.getElementById('out');
const dot = document.getElementById('dot');
const badge = document.getElementById('badge');
const foot = document.getElementById('foot');
const RUN = '__RUNID__';
const chunks = [];

/* 「已就绪：/ 未就绪：」这类状态行自带颜色 —— 让 Python 那边不必关心 CSS。 */
function toneOf(text) {
  if (text.indexOf('已就绪：') === 0) return 'ok';
  if (text.indexOf('未就绪：') === 0) return 'bad';
  return '';
}

function write(text, cls) {
  chunks.push(text);
  const span = document.createElement('span');
  const tone = cls || toneOf(text);
  if (tone) span.className = tone;
  span.textContent = text;
  out.appendChild(span);
  // 跟着滚：跑长脚本时不用手动往下拉
  const scroller = document.scrollingElement;
  if (scroller) scroller.scrollTop = scroller.scrollHeight;
}

/* 秒数一律带单位：别让人对着 1715 自己换算。 */
function secs(ms) {
  return (ms < 9500 ? (ms / 1000).toFixed(2) : (ms / 1000).toFixed(1)) + 's';
}
/* 把这次运行的输出**交回宿主**。
   模型看不见沙箱里发生了什么 —— 不回传，它就只能凭想象说"跑出来了"：
   实测它声称 numpy/scipy 可用，而这块面板上写着 No module named 'numpy'。
   回传之后宿主能把输出摆出来，也能一键把它发回给模型。 */
function report(ok) {
  try {
    parent.postMessage({ qfRun: RUN, ok: ok, text: chunks.join('') }, '*');
  } catch (err) { /* 不在 iframe 里（直接打开这个页面）就没什么可回传的 */ }
}
(async () => {
  let bootMs = 0;
  let packMs = 0;
  try {
    // 三段分别计时：运行时 / 依赖 / 代码。
    // 合并成"用时 1.7s"没有用 —— 那个数看不出慢在哪（实测用户就问过
    // "一秒多有点慢"，而那一秒其实是 scipy 第一次 import 的固有成本）。
    const t0 = performance.now();
    const py = await loadPyodide({ indexURL: '__INDEX__' });
    bootMs = performance.now() - t0;
    py.setStdout({ batched: (s) => write(s + '\\n') });
    py.setStderr({ batched: (s) => write(s + '\\n', 'bad') });
    foot.textContent = '运行时 ' + secs(bootMs) + ' · 正在准备依赖…';

    const want = __PACKAGES__;
    const t1 = performance.now();
    if (want.length) {
      try {
        await py.loadPackage(want);
      } catch (err) {
        // 加载失败要**说出来**：悄悄降级的后果是代码里 import 失败，
        // 而模型以为装好了（实测发生过）
        write('依赖加载失败：' + want.join('、') + ' —— ' + String((err && err.message) || err) + '\\n', 'bad');
      }
    }
    packMs = performance.now() - t1;

    // 把沙箱的能力**报出来**（哪几个包在、版本多少），顺手填右上角那枚徽标。
    // 这段比代码本身还重要：输出会**自动回填**给服务端，于是模型下一轮自己
    // 就能读到"这个环境有什么"，不必猜、也不必等人转述。
    const info = py.runPython(
      'import sys, importlib\\n' +
      '_bits = ["Python " + sys.version.split()[0]]\\n' +
      'for _name in __CHECK__:\\n' +
      '    try:\\n' +
      '        _mod = importlib.import_module(_name)\\n' +
      '        _ver = getattr(_mod, "__version__", "?")\\n' +
      '        print("已就绪：" + _name + " " + _ver)\\n' +
      '        _bits.append(_name + " " + _ver)\\n' +
      '    except Exception as _exc:\\n' +
      '        print("未就绪：" + _name + " —— " + str(_exc))\\n' +
      '        _bits.append(_name + " 未就绪")\\n' +
      '" · ".join(_bits)'
    );
    badge.textContent = String(info);
    dot.className = 'dot is-ok';

    const t2 = performance.now();
    await py.runPythonAsync(__CODE__);
    const codeMs = performance.now() - t2;

    const line = '运行时 ' + secs(bootMs) + ' · 依赖 ' + secs(packMs) + ' · 代码 ' + secs(codeMs);
    write('\\n— ' + line + '\\n', 'meta');
    foot.textContent = line;
    report(true);
  } catch (err) {
    dot.className = 'dot is-bad';
    badge.textContent = '出错了';
    write(String((err && err.message) || err) + '\\n', 'bad');
    foot.textContent = '运行时 ' + secs(bootMs) + ' · 失败';
    report(false);
  }
})();
</script></body></html>"""


#: **常驻运行壳**：一个页面启动一次，之后反复收代码执行。
#:
#: 为什么要有它（上面那份"一段脚本一个壳"是旧做法）：Pyodide 的启动是**每次都要重来**的
#: —— 下载 wasm、初始化解释器、再 import numpy/scipy，实测第一段脚本要一秒多，换一段又要
#: 重来一次。用户的话是"跑 python 脚本非常慢，你研究一下"。壳常驻之后，那份成本只付**一次**
#: （页面打开时），后面每次运行就只剩执行本身。
#:
#: 协议（与宿主之间）：
#:   * 壳 → 宿主：`{qfReady: true}`     —— 运行时 + 依赖都就绪，可以派活了
#:   * 宿主 → 壳：`{qfRun, qfCode}`     —— 请跑这段代码
#:   * 壳 → 宿主：`{qfRun, ok, text}`   —— 这一次的输出（与旧壳同形状，宿主那条回填不用改）
#:
#: 每次运行**清掉上一段留下的顶层名字**：脚本之间不该互相看见对方的变量
#: （同一个人在同一个壳里连着跑两段，第二段能看到第一段的 `A`、`B` 只会造成误判）。
_PYODIDE_SHELL = r"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<title>Python 运行壳</title>
<script src="__INDEX__pyodide.js"></script>
<script>
/* 常驻运行壳：启动一次，之后按消息执行。它**不出现在画面上**（宿主把它放成 1×1）。 */
var QUEUE = [];        // 还没就绪时先排着
var PY = null;         // 就绪后的 Pyodide
var CHUNKS = [];       // 当前这次运行的输出
var CURRENT = '';      // 当前这次运行的 runId

function send(payload) {
  try { parent.postMessage(payload, '*'); } catch (err) { /* 不在 iframe 里就算了 */ }
}

function write(text) { CHUNKS.push(text); }

/** 这一轮画出来的图（matplotlib）——壳自己收走，不用脚本 savefig。
 *
 * 为什么这么做：面板只能显示文本，而"把 PNG 打成 base64 再 print"对模型是灾难
 *（一屏几千个字符的乱码）。让壳在图省下来的地方收，模型那边只看到"画了几张"。
 *
 * **只在 import 过 matplotlib 时才动**：否则每次运行都白 import 它（几百毫秒）。
 * 最多 3 张：base64 会跟着回传、存进库，多了不合适。
 */
function collect_figures() {
  var out = [];
  try {
    if (!PY.runPython('"matplotlib" in __import__("sys").modules')) return out;
    out = PY.runPython(
      'import base64, io\n' +
      'import matplotlib.pyplot as plt\n' +
      '_imgs = []\n' +
      'for _n in plt.get_fignums()[:3]:\n' +
      '    _b = io.BytesIO()\n' +
      '    plt.figure(_n).savefig(_b, format="png", dpi=110, bbox_inches="tight")\n' +
      '    _imgs.append(base64.b64encode(_b.getvalue()).decode("ascii"))\n' +
      'plt.close("all")\n' +
      '_imgs'
    ).toJs();
  } catch (err) {
    /* 出图失败不影响这一轮的输出：图没了，字还在 */
    out = [];
  }
  return out || [];
}

async function execute(job) {
  CURRENT = String(job.qfRun || '');
  CHUNKS = [];
  var code = String(job.qfCode || '');
  // 跑之前先记下顶层有哪些名字，跑完把**新出现的**删掉：两段脚本不共享变量。
  var before = [];
  try { before = PY.runPython('list(globals().keys())').toJs(); } catch (err) { before = []; }
  try {
    var t0 = performance.now();
    // **按 import 现装包**：`loadPackagesFromImports` 扫代码里的 import 语句，
    // 本机 indexURL 旁边有的（pandas / matplotlib 那一拨）就当场装上；
    // lock 里没有的（比如 torch）它静默跳过，等真正执行时报错，报错也更准。
    // 没用到的人一个字节都不下 —— 这正是"用到再下"那条规矩的落点。
    try {
      await PY.loadPackagesFromImports(code);
    } catch (err) {
      /* 装不上不在这里断：让脚本照跑，import 自己会报出清楚的错 */
    }
    // 点名要装的（动态导入那种扫不出 import 的写法）——`packages` 参数走这条路
    var asked = job.qfPackages || [];
    if (asked.length) await PY.loadPackage(asked);
    // **让 matplotlib 用 Agg 后端**再让脚本 import 它。
    //
    // Pyodide 默认给的 canvas 后端要把画布插进**页面 DOM**，而壳是 1×1 的隐藏
    // iframe —— 实测它连 `plt.close("all")` 都会抛
    // `AttributeError: 'NoneType' object has no attribute 'parentNode'`，
    // 图也就收不上来。Agg 是纯内存渲染，`savefig` 直接出 PNG，正合收图的路子。
    //
    // 只在**这一轮真的用到** matplotlib 时才动（扫一遍代码）：不用它的脚本
    // 不该为此多花一次 import。必须在脚本 import 之前设 —— import 之后就改不动了。
    if (/matplotlib|pyplot|mpl\./.test(code)) {
      try {
        PY.runPython(
          'import sys\n' +
          'if "matplotlib" not in sys.modules:\n' +
          '    import matplotlib\n' +
          '    matplotlib.use("Agg")\n' +
          // **把中文那几行再确认一遍**：环境准备里设过一次，但那不够 —— 壳是**常驻**的，
          // 上一条脚本的副作用会留在这个解释器里（换 style、`rcdefaults()`、自己改
          // `font.sans-serif` 都会把中文弄丢）。只设一次的话，翻车时机是"第二次运行"，
          // 看着就像**有时行有时不行**（用户原话）。复核用模块上记住的名字，
          // 不重新解析那 4.3MB 字体。
          'import matplotlib\n' +
          '_n = getattr(matplotlib, "_qf_cjk", "")\n' +
          'if _n:\n' +
          '    matplotlib.rcParams["font.sans-serif"] = [_n] + [f for f in matplotlib.rcParams["font.sans-serif"] if f != _n]\n' +
          '    matplotlib.rcParams["font.serif"] = [_n] + [f for f in matplotlib.rcParams["font.serif"] if f != _n]\n' +
          '    matplotlib.rcParams["axes.unicode_minus"] = False\n' +
          '    matplotlib.rcParams["mathtext.fontset"] = "custom"\n' +
          '    matplotlib.rcParams["mathtext.rm"] = _n\n' +
          '    matplotlib.rcParams["mathtext.it"] = _n\n' +
          '    matplotlib.rcParams["mathtext.bf"] = _n\n' +
          '    matplotlib.rcParams["font.cursive"] = [_n]'
        );
      } catch (err) { /* 没装上就算了：脚本自己 import 时会报出真正的错 */ }
    }
    await PY.runPythonAsync(code);
    // **回传的 text 里只放脚本自己的 stdout/stderr**：壳的内部账（执行耗时、
    // 环境自述、启动开销）一个字都不许混进来 —— 那是壳的事，不是这段脚本的输出。
    //（用户："python 沙箱的输出里面怎么会有这个？""这个东西也不该出现在输入里"。）
    // 计时照旧算，但作为 `ms` 单独回传：要显示也由面板在**它自己的位置**显示。
    var images = collect_figures();
    send({
      qfRun: CURRENT,
      ok: true,
      text: CHUNKS.join(''),
      ms: Math.round(performance.now() - t0),
      images: images,
    });
  } catch (err) {
    write(String((err && err.message) || err) + '\n');
    send({ qfRun: CURRENT, ok: false, text: CHUNKS.join('') });
  } finally {
    delete_names(before);
    CURRENT = '';
    CHUNKS = [];
  }
}

/* 清掉这次运行新加的顶层名字（不动 import 进来的模块名，那些是运行时自己的）。 */
function delete_names(before) {
  try {
    var now = PY.runPython('list(globals().keys())').toJs();
    var keep = {};
    for (var i = 0; i < before.length; i++) keep[before[i]] = 1;
    var doomed = [];
    for (var j = 0; j < now.length; j++) {
      var name = String(now[j]);
      if (keep[name] || name.indexOf('_') === 0) continue;
      doomed.push(name);
    }
    if (doomed.length) {
      PY.runPython('for _n in ' + JSON.stringify(doomed) + ':\n    globals().pop(_n, None)');
    }
  } catch (err) { /* 清理失败不影响结果：下一次运行只是多带一个旧名字 */ }
}

window.addEventListener('message', function (event) {
  var data = event.data || {};
  if (!data.qfCode) return;
  if (!PY) { QUEUE.push(data); return; }
  execute(data);
});

(async () => {
  var t0 = performance.now();
  PY = await loadPyodide({ indexURL: '__INDEX__' });
  // 解释器这一笔单独记：报账时要能说清"哪一段可以被省掉"（总时长仍由 t0 算）
  var pyMs = Math.round(performance.now() - t0);
  PY.setStdout({ batched: function (s) { write(s + '\n'); } });
  PY.setStderr({ batched: function (s) { write(s + '\n'); } });

  // **压掉两类噪音警告**（都实测撞过）：
  //   * DeprecationWarning —— 写给库作者看的（每次 import pandas 一坨 PyArrow 预告）；
  //   * 「Matplotlib is currently using agg … cannot show the figure」—— 脚本里
  //     写了 plt.show() 就会冒出来。图本来就由壳收走贴在面板上（见 collect_figures），
  //     show 在这个环境里没有意义，这条提示对用户也毫无价值。
  // 只压这两类：真正的错误是异常，不走 warning 通道，一个字都不会被吃掉。
  try {
    PY.runPython(
      'import warnings\n' +
      'warnings.filterwarnings("ignore", category=DeprecationWarning)\n' +
      'warnings.filterwarnings("ignore", message=".*non-GUI backend.*")'
    );
  } catch (err) { /* 压不掉就算了，不影响跑 */ }

  var want = __PACKAGES__;
  var packMs = 0;
  var packWarning = '';
  if (want.length) {
    var t1 = performance.now();
    try {
      await PY.loadPackage(want);
    } catch (err) {
      /* 装不上要**报出来**：悄悄降级的代价是"看起来装了、其实 import 失败"。
       * 攒着跟"就绪"一起发（提前发会让宿主收到两次就绪，见下面那段注释）。 */
      packWarning = '依赖加载失败：' + want.join('、') + ' —— ' + String((err && err.message) || err);
    }
    packMs = performance.now() - t1;
  }

  // **lock 之外的额外包**（schemdraw：画电路图那个）。
  //
  // `loadPackage` 找不到它们 —— Pyodide 只认自己 lock 里那 310 个。所以 `make vendor`
  // 把这些 wheel 放在**同一个目录**，这里用 micropip 从**本机** URL 装：仍然不联网，
  // 与别的包一个规矩。装在预载之后、自述之前，所以"这次到底装上了没有"当场就知道。
  var extra = __EXTRA__;
  var extraWarning = '';
  if (extra.length) {
    var t2 = performance.now();
    try {
      // **micropip 自己也得先装上**：它在 lock 里（本机就有），但不默认加载 ——
      // 直接 `import micropip` 会 ModuleNotFoundError（实测就栽在这儿）。
      await PY.loadPackage('micropip');
      await PY.runPythonAsync(
        'import micropip\n' +
        'await micropip.install(' +
        JSON.stringify(extra.map(function (n) { return '__INDEX__' + n; })) +
        ')'
      );
    } catch (err) {
      // 装不上要**报出来**（悄悄降级的代价是"看起来有、import 时才发现没有"），
      // 但**不在这里发 qfReady** —— 记号攒着跟自述一起发，不然宿主那边会收到两次
      // "就绪"、状态乱掉（实测：第二次把 info 覆盖成 undefined）。
      extraWarning = '额外包没装上：' + extra.join('、') + ' —— ' + String((err && err.message) || err);
    }
    packMs += performance.now() - t2;
  }

  // **绘图后端必须在任何人 import matplotlib 之前定下来**（为什么非得 Agg：见 execute
  // 里那段说明 —— 壳是 1×1 的隐藏 iframe，默认的 canvas 后端要把画布插进 DOM，收图会炸）。
  //
  // 修的是顺序 bug：这一步从前只写在 `execute` 的守卫里，而守卫的条件是"matplotlib 不在
  // sys.modules 里"——下面那段环境自述恰好会 `__import__("matplotlib")`，于是守卫之后
  // 恒假、`use("Agg")` 从未真正执行过（注释说的那件事一直是落空的）。
  // 现在放在自述**之前**；顺手把 pyplot 的首个 import（那次 826ms 里的大头）也留在这里 ——
  // 这正是用户说的"预装包合并在环境准备里"：第一次真的画图时，这些都不必再等。
  var warmMs = 0;
  var fontName = '';
  var fontWarning = '';
  var hasPlot = want.some(function (n) { return n === "matplotlib"; });
  if (hasPlot) {
    var t3 = performance.now();
    try {
      PY.runPython(
        "import matplotlib\n" +
        "matplotlib.use(\"Agg\")\n" +
        "import matplotlib.pyplot"
      );
    } catch (err) { /* 没装上就算了：脚本自己 import 时会报出真正的错 */ }
    warmMs = performance.now() - t3;

    // **中文字体**：脚本里写中文标题、图例、轴标签时，别变成一屏"豆腐块"。
    //
    // 从前这里没有中文字形，所以提示词一直在劝模型"图里别写中文"（那是实话，但很碍事）。
    // 用户要求把中文装上，两边都装：正文里的图走 LaTeX 那份（见 vendor/tikzjax），
    // 沙箱里的图走这一份。
    //
    // 为什么要**另取一份 TTF**：matplotlib 不认 woff2（浏览器那套它读不了）。这份是
    // vendor/cjk/gbsn00lp.ttf（构建时搬进 `assets/fonts/`）—— 同一份 Arphic 宋体、
    // 同样的码点范围，所以用户在正文图与沙箱图里看到的是同一个字体的汉字。
    //
    // 为什么地址是 `/assets/fonts/` 而不是 tikzjax 那边：壳是**不透明源**的独立文档
    //（sandbox 里没给 `allow-same-origin`），它的 fetch 带 `Origin: null`，会被 CORS 拦
    //（实测：`blocked by CORS policy`）。而 `main.py` 只给 `/assets/fonts/` 开了 CORS
    //（当初是为了沙箱里那份等宽字体，同一个道理）。
    //
    // 两处细节都不是可选项：
    //   * 把族名**排到字体栈最前**，否则 matplotlib 仍然用默认的那套（没有汉字）；
    //   * 关掉 `axes.unicode_minus` —— 不关的话负号会画成方框，是 CJK 场景的老毛病。
    //
    // 克制照旧：**计费/2G 网络不取**（4MB 不该在人没要的时候花掉他的流量）。取不到、
    // 或登记失败都只是"中文会退回豆腐块"，不影响别的任何东西 —— 所以失败不抛，只记账。
    var conn = navigator.connection || navigator.mozConnection || navigator.webkitConnection;
    var stingy = conn && (conn.saveData || /(^|-)2g$/.test(conn.effectiveType || ''));
    if (!stingy) {
      var t4 = performance.now();
      try {
        var resp = await fetch('__ORIGIN__/assets/fonts/gbsn00lp.ttf');
        if (resp.ok) {
          var buf = await resp.arrayBuffer();
          PY.FS.writeFile('/tmp/gbsn00lp.ttf', new Uint8Array(buf));
          fontName = PY.runPython(
            "import matplotlib, matplotlib.font_manager as _fm\n" +
            "_fm.fontManager.addfont('/tmp/gbsn00lp.ttf')\n" +
            "_nf = _fm.FontProperties(fname='/tmp/gbsn00lp.ttf').get_name()\n" +
            "matplotlib.rcParams['font.sans-serif'] = [_nf] + [__f for __f in matplotlib.rcParams['font.sans-serif'] if __f != _nf]\n" +
            "matplotlib.rcParams['font.serif'] = [_nf] + [__f for __f in matplotlib.rcParams['font.serif'] if __f != _nf]\n" +
            "matplotlib.rcParams['axes.unicode_minus'] = False\n" +
            // **mathtext 是另一套字体栈** —— 这一条是实测补上的：对数轴的刻度（`10^{-1}`）
            // 与图里的 `$...$` 都由 mathtext 排，`font.sans-serif` **管不到**它。只设上面
            // 那几行的话，负指数里的减号（U+2212）会变成"哑符号"，日志里一串
            // `Font 'default' does not have a glyph for '-' [U+2212]`。
            // 用户拿对数轴试了出来，模型为此来回改了四轮才对 —— 这种事该环境准备好。
            "matplotlib.rcParams['mathtext.fontset'] = 'custom'\n" +
            "matplotlib.rcParams['mathtext.rm'] = _nf\n" +
            "matplotlib.rcParams['mathtext.it'] = _nf\n" +
            "matplotlib.rcParams['mathtext.bf'] = _nf\n" +
            // `font.cursive` 在 Pyodide 这份 matplotlib 里是空的 → mathtext 一探字体就吐
            // `findfont: Font family ['cursive'] not found`（**不是错**，但看着像 ✗，
            // 用户的输出里就出现过）。指到同一份字体，噪音就没了。
            "matplotlib.rcParams['font.cursive'] = [_nf]\n" +
            // 名字留在模块上：**每次运行前**要把这几行再确认一遍（脚本可能 `rcdefaults()`），
            // 而重新解析一遍那 4.3MB 字体没必要 —— 记下来就够（见 execute 里那段）。
            "matplotlib._qf_cjk = _nf\n" +
            "_nf"
          );
        }
      } catch (err) {
        fontWarning = '中文字体没装上：' + String((err && err.message) || err);
      }
      warmMs += performance.now() - t4;
    }
  }

  // 把环境报给宿主（它会把这几行摆进第一次运行的输出里）——模型因此知道
  // 这个沙箱里有什么、版本多少，不必猜。
  var info = '';
  try {
    info = PY.runPython(
      'import sys\n' +
      '_bits = ["Python " + sys.version.split()[0]]\n' +
      'for _name in __CHECK__:\n' +
      '    try:\n' +
      '        _m = __import__(_name)\n' +
      '        _bits.append(_name + " " + getattr(_m, "__version__", "?"))\n' +
      '    except Exception:\n' +
      '        _bits.append(_name + " 未就绪")\n' +
      '" · ".join(_bits)'
    );
  } catch (err) {
    info = String((err && err.message) || err);
  }
  // 让模型**知道沙箱里中文排得出来** —— 不然它会照旧避开图里的中文（提示词那一侧已经从
  // "图里别写中文"改成允许）。自述会进第一次运行的输出，这是它唯一能读到"有中文字体"的地方。
  if (fontName) info += ' · 中文字体 ' + fontName;
  send({
    qfReady: true,
    info: info,
    // `bootMs` = **环境准备总共花了多少**（含解释器、依赖、绘图预热），
    // `pyMs` = 其中解释器那一笔，`packMs` = 依赖，`warmMs` = 绘图预热
    //（含 Agg + pyplot 首个 import + 中文字体那一笔 —— 都属"让绘图能用"的准备）。
    // 从前只报总时长，看着像"解释器很慢"；分开报是为了让"哪一段能被省掉"看得见。
    bootMs: Math.round(performance.now() - t0),
    pyMs: pyMs,
    packMs: Math.round(packMs),
    warmMs: Math.round(warmMs),
    // 三条警告（预载失败 / 额外包失败 / 中文字体失败）攒到这里一起报 —— 只发一次"就绪"
    warning: [packWarning, extraWarning, fontWarning].filter(Boolean).join('；') || undefined,
  });

  // 就绪前排队的那几段，现在补跑
  while (QUEUE.length) execute(QUEUE.shift());
})();
</script></head><body></body></html>"""


def shell_stamp() -> str:
    """常驻壳那份页面的**运行时指纹**（内容哈希）。

    存在理由：壳是"页面打开时建一次、之后一直用"，而它的环境准备里装着**要装一次**的
    东西（matplotlib、中文字体）。壳的页面由服务端现渲染，跟 `make web` 无关 —— 于是
    只盯构建戳的话，改了 `_PYODIDE_SHELL` 之后：构建戳没变 → 已经打开的页面不刷新 →
    页面里的壳还是旧的 → 用户问"中文能画了吗"，模型在**旧壳**里跑出"一个中文字形都没有"，
    就很诚实地回答"这台机器画不出汉字"（实测就这么绕了一圈，白折腾一轮）。
    有了这个指纹，`/api/build` 与页面各自报一份，对不上就自己刷新。
    """
    import hashlib

    return hashlib.sha1(shell_page().encode("utf-8")).hexdigest()[:12]


def shell_page() -> str:
    """常驻运行壳的那份 HTML（宿主拿去做隐藏 iframe 的 `srcdoc`）。"""
    return (
        # 壳启动时只装**预载**那一拨；按需的那拨由壳在执行前看 import 现装
        #（`loadPackagesFromImports`）—— 所以两张名单注入的都是预载这一份：
        # 一份给 `loadPackage`，一份给启动时那句"版本自述"（报的是**已经装好的**）。
        _PYODIDE_SHELL.replace("__INDEX__", pyodide_base() or "")
        .replace("__PACKAGES__", json.dumps(list(PRELOAD_PACKAGES), ensure_ascii=False))
        .replace("__CHECK__", json.dumps(list(PRELOAD_PACKAGES), ensure_ascii=False))
        # lock 之外的额外 wheel（壳里用 micropip 从本机装，见 EXTRA_WHEELS）
        .replace("__EXTRA__", json.dumps(list(EXTRA_WHEELS), ensure_ascii=False))
    )


def _python_page(title: str, code: str, packages: list[str], run_id: str) -> str:
    return (
        _PYODIDE_PAGE.replace("__TITLE__", html.escape(title)[:80])
        .replace("__INDEX__", pyodide_base() or "")
        .replace("__RUNID__", html.escape(run_id)[:40])
        .replace("__PACKAGES__", json.dumps(packages, ensure_ascii=False))
        .replace("__CHECK__", json.dumps(list(packages), ensure_ascii=False))
        .replace("__CODE__", json.dumps(code, ensure_ascii=False))
    )


def run_python(db, args, ctx=None) -> dict:  # noqa: ANN001
    """在沙箱里**真跑**一段 Python（Pyodide），结果落在演示面板里。

    ## 为什么要有它

    用户说"跑一段脚本"时，模型不该推辞、也不该让他自己去写 HTML ——
    它只要把 Python 交出来，Pyodide 的样板由这里负责。

    ## 能跑什么、跑不了什么

    * 标准库、纯计算、文本输出：没问题。
    * **能用的第三方包就是 `PRELOAD_PACKAGES` 那一份**（numpy / scipy / pandas /
      matplotlib / sympy / networkx，还有 `EXTRA_WHEELS` 里的 schemdraw），**全部在
      环境准备时一次装好** —— 脚本里直接 `import` 就行，不用等下载，也不用点名。
      （`packages` 只留给**动态导入**那种壳扫不出来的写法。）
      **名单一律以那两个常量为准** —— 这里从前按印象抄过一份"numpy 与 scipy，
      别的装不了"，结果 matplotlib 进了预载之后这句话还留着，模型照着它拒绝出图。
      —— 所以这处只指名字、不再复述名单。
    * 名单外的装不上：要编译、要系统库的（`torch` 之类）都没有。
    * **画图直接画** ✓：`plt.plot(...)` / `plt.hist(...)` 之后**不用** `savefig`、
      也不用 base64 —— 壳会把图收走贴在面板上给用户看（最多 3 张）。
      下面这两行是踩过的坑，照抄：**图里可以写中文**（壳的环境准备里装好了一份简体宋体，
      标题、图例、轴标签都排得出来），用**常见简体字**即可 —— 生僻字、繁体、日文假名
      可能没有字形（那些会显示成方框）。
    * **电路图用 `schemdraw`**（本机装好了，专治"手算元件坐标"那种笨活）——
      网表式地拼：给元件与连接，它负责排版与符号。**别用 plt.plot 手画电阻**，
      那种图又难看、改一个参数就得重排。照这个骨架写：

          import schemdraw
          import schemdraw.elements as elm
          d = schemdraw.Drawing()
          d += elm.Resistor().right().label('R1 = 10k')     # 锯齿是 ANSI 风
          d += elm.Capacitor().down().label('C1')
          d += elm.Ground()
          d.draw()

      要 IEC 的**矩形**电阻就换 `elm.ResistorIEC()`（国内教材多用它）。
      库很全：电阻/电容/电感/二极管/三极管(BJT/MOS)/运放/受控源/开关/变压器…
      出图与 matplotlib 一样 —— **不用 savefig**，壳会自己收走贴在面板上。
    * **符号推导用 `sympy`**（本机有）：模电里"把 A_v(s) 化简成标准二阶形式"
      这种活计，数值算给不出一支带参数的表达式，符号算才行。参数别太多
      （四五个以内），多了它化简不出来、比手推还难看。
    * **跑不了**：要编译或依赖系统库的（`torch`）、要读本机文件或连数据库的 ——
      沙箱里没有文件系统，也没有用户的数据。

    ## 输出会**等它跑完**再回到你手里（这条最要紧）

    这次工具调用**不会立刻返回**：它会等页面把脚本跑完（几毫秒到几秒），
    输出就作为这次调用的结果交给你 —— 所以：

    * **拿到输出再说话**：直接看结果下结论，那是真跑出来的东西。
    * **不许**说"我已经交进去跑了""跑完我下一轮看看""你那边看面板"这类话 ——
      那一轮就是现在，输出几秒内就到（实测模型说过这种话，用户的原话是：
      "让 agent 等命令返回了再说话"）。
    * 也**不许**在没有输出的情况下声称结果（实测发生过：声称 numpy 可用，
      而面板上写着 No module named 'numpy'）。要是这条结果里写着"没等到沙箱的输出"，
      就如实说没等到。
    """
    code = str(args.get("code") or "").strip()
    if not code:
        return {"error": "code 不能为空（给我要跑的 Python 源码）。"}
    if len(code) > CODE_MAX_CHARS:
        return {
            "error": f"代码太长（{len(code)} 字，上限 {CODE_MAX_CHARS}）—— 精简到能说明问题就行。"
        }

    if pyodide_base() is None:
        # 运行时不在本机（第一次用，缓存还没有）——**在后台开始取**，这一轮如实说明。
        # 不在这里同步下载：76M 会把这次工具调用挂住，界面看起来像卡死。
        started = heavy_deps.start_prefetch()
        return {
            "error": (
                "跑 Python 的运行时还没到位（它 76M，不进安装包，要本机取一次并校验）。"
                + ("已经在后台开始取了，过一会儿再让我跑这段代码。" if started else "等它取完再试。")
            )
        }

    asked = [str(item).strip() for item in (args.get("packages") or []) if str(item).strip()][:4]
    # `packages` 的语义现在是"**保证这几个装上**"，而且只在名单里挑。
    #
    # 原先这里还按代码里的**字样**猜（出现 "scipy" 就装 47MB）—— 现在壳里有
    # `loadPackagesFromImports`，真 import 什么就装什么，比猜准得多，所以猜的那步
    # 退休了。`packages` 只留给"动态导入"（`__import__("scipy")`）这种壳扫不出来的写法。
    asked_keys = [name.lower().strip().split(".")[0] for name in asked]
    packages = [name for name in PYODIDE_PACKAGES if name in asked_keys]

    # 名单外的剔掉并告诉模型。Pyodide 能不能装某个包是**写死的事实**，猜不出来，
    # 而猜错的代价是"看起来装上了、跑起来 No module named"（实测发生过）。
    skipped: list[str] = []
    for name in asked:
        key = name.lower().strip().split(".")[0]  # scipy.signal 这种也认成 scipy
        if key and key not in PYODIDE_PACKAGES and key not in skipped:
            skipped.append(key)

    title = str(args.get("title") or "").strip()[:80] or "Python 运行结果"
    run_id = uuid.uuid4().hex[:12]
    # 这段是**回给模型的话**，包名单同样从常量生成（与注册表那份说明同一个来由：
    # 名单会变、散文不会自己跟着变）。从前固定写"其中 <预载> 开箱就有；另有 <按需>
    # —— 本机备着…"，而按需那份现在**是空的** —— 那句话会渲染成"另有  —— 本机备着"，
    # 等于没说；更要紧的是它会让模型把"开箱就有"误解成"只有这几个"。
    note = (
        "代码已经交出去跑了，**这一次调用会等它跑完**（几毫秒到几秒）——"
        "输出会作为这次调用的结果给你，所以**拿到结果再说话**，可以直接下结论。"
        "**不要**把源码贴进正文，**也别说**「已经交进去跑了」「下一轮再看」这类话"
        "（那一轮就是现在）；更不许在没有输出的情况下声称跑出了什么。"
        "沙箱里**开箱就有**的包：" + "、".join(PRELOAD_PACKAGES) + "。"
        + (
            "另有本机备着、import 到才装的：" + "、".join(OPTIONAL_PACKAGES) + "。"
            if OPTIONAL_PACKAGES
            else ""
        )
        + "名单外的装不上。"
        "别用 `__import__(\"...\")` 那种动态写法（壳扫不出 import，装不上）——"
        "要这种写法就在 `packages` 里点名。"
        "**画图直接 `plt.plot(...)` 就行**：壳会把图收走贴在面板上，"
        "不用 savefig、不用 base64；图里**可以写中文**（中文标题、图例、轴标签、刻度都行，"
        "常见简体字有字形），**字体那几行也不用你写**（壳设好了，每次运行前复核）。"
    )
    if skipped:
        note += (
            " 你要的 " + "、".join(skipped) + " 不在这个子集里，**没有装** —— "
            "换名单里的包，或者用标准库自己实现；也可以如实告诉他这个沙箱装不了。"
        )
    # 交给**常驻运行壳**（见 `_PYODIDE_SHELL`）：这里不再生成"一段脚本一个页面"。
    # 零件只带 runId，脚本原文走工具入参（宿主从那里取），宿主把 code post 给那个壳、
    # 壳跑完回传。老消息里的零件仍然带 `html`（那时是一段脚本一个壳）——宿主对
    # 那一种走兼容路径，所以 `_python_page` 与 `PYODIDE_PACKAGES` 都还得留着。
    #
    # `await_run`：让 agent 循环**停下来等**这次运行（见 agent_loop 里那段注释）。
    # 只在这一条上打开 —— 别的工具（查资料、推题）都是自己就出结果的，不需要等。
    return {
        "demo": {
            "title": title,
            "runId": run_id,
            # 点名要装的包（`packages` 参数那条路）——宿主会把它连同代码一起交给壳
            #（壳里叫 `qfPackages`）。壳自己还会按 import 现装别的，不靠这一项。
            "packages": packages,
        },
        "await_run": True,
        "note": note,
    }


# ---------------------------------------------------------------- 演示沙箱


# 演示 HTML 的上限：这份东西会**落进零件的库**、每次读会话都发给前端，
# 所以它得小。80KB 足够画一个像样的数据流/切分/流水线可视化。
DEMO_MAX_CHARS = 80_000

# 演示套件（React + JSX + d3 + 我们那层组件）由服务端统一注入，见 `_demo_page`。
# 顺序有讲究：经典脚本按出现顺序执行 —— Tailwind 先（它的预置样式要被 kit CSS 盖住）、
# React/ReactDOM 在 kit 之前、Babel 在模型的 `text/babel` 之前。
#: 产物里 `assets/` 下的公共资源（KaTeX 就在这儿，见 build_web.py）
DEMO_ASSET_DIR = "__ORIGIN__/assets/"
DEMO_KIT_DIR = "__ORIGIN__/assets/demo-kit/"
DEMO_KIT_FILES = (
    "tailwind.js",
    "react.js",
    "react-dom.js",
    "htm.js",
    "d3.js",
    "babel.js",
    "qf-kit.js",
)
DEMO_VENDOR_KIT = Path(__file__).resolve().parents[2] / "vendor" / "demo-kit"
DEMO_SERVED_KIT = Path(__file__).resolve().parents[1] / "web" / "assets" / "demo-kit"

# ---------------------------------------------------------------- 沙箱的 Python 子集
#
# **刻意只支持一个子集**：Pyodide 的包要么是纯 WASM 轮子、要么根本装不上
# （要编译或有 C 扩展的都不行），而且每多一个就是几十 MB 跟着构建走
# （scipy 一份 47MB）。所以这里写死一个短名单，有需求再酌情加 ——
# 宁可是"明确说不行"，也不要"看起来能装、跑起来 No module named"。
#
# 与 `tools/vendor.py` 的 `PYODIDE_PACKAGES` 对应：那边负责取回（含依赖，
# 如 scipy → numpy + openblas），这边负责加载。
#: 壳**启动时就装好**的包 —— 现在是**全部**（用户："所有的 python 包都务必在运行壳里
#: 自动静默预加载"）。
#:
#: 原先 pandas / matplotlib 是"脚本 import 到了才装"：省流量，但第一次 import 要等
#: 3.5 秒左右 —— 实测就是用户说的"这次执行速度有点慢"。壳常驻，这笔钱整个会话
#: 只付一次，索性一起装在前面（壳启动那 2~3 秒里顺带完成，用户感知不到多出来的部分）。
PRELOAD_PACKAGES = (
    "numpy",
    "scipy",
    "pandas",
    "matplotlib",
    # sympy / networkx：本机 lock 里就有（纯 Python）。加它们是因为 EE 那类课要的东西
    # 与"算个数"不同 —— 模电里大量动作是**推导**（小信号等效 → KCL/KVL → A_v(s) 化简
    # 成标准二阶形式），数值算给不出一支带参数的表达式。networkx 顺手给网表/图论用。
    "sympy",
    "networkx",
)

#: 本机备着、**真用到才装**的包 —— 现在是空的（都进预载了）。
#: 留这条是因为那条机制还在（壳里的 `loadPackagesFromImports`）：哪天想放回去，
#: 把名字挪过来就行。
OPTIONAL_PACKAGES: tuple[str, ...] = ()

#: 沙箱里**能用**的全部包 = 预载 + 按需。`packages` 参数与工具说明都认这一份。
PYODIDE_PACKAGES = PRELOAD_PACKAGES + OPTIONAL_PACKAGES

#: **lock 里没有、但我们要的包**（打包好的 wheel 文件名，`make vendor` 会放到
#: indexURL 旁边）。Pyodide 的 `loadPackage` 只认它 lock 里那 310 个，所以这些得
#: 走 `micropip` 从**本机** URL 装 —— 壳里就是这么干的（见 `_PYODIDE_SHELL`）。
#:
#: `schemdraw` = 画电路图（元件符号 / 连线 / 节点标号，网表式地拼，不用手算坐标）。
#: 版本跟 `tools/vendor.py` 的 `EXTRA_WHEELS` 是**同一份名单**，改的时候两处一起改。
EXTRA_WHEELS: tuple[str, ...] = ("schemdraw-0.23-py3-none-any.whl",)
PYODIDE_PACKAGE_URL = {"numpy": "https://pyodide.org/en/stable/usage/packages-in-pyodide.html"}


def _demo_kit_ready() -> bool:
    """套件是否已就位（`make vendor` 取回、构建拷进 /assets/demo-kit/）。"""
    for directory in (DEMO_SERVED_KIT, DEMO_VENDOR_KIT):
        if (directory / "react.js").is_file() and (directory / "qf-kit.js").is_file():
            return True
    return False


def _demo_theme(db: Any) -> str:  # noqa: ANN001
    """用户当前的主题（设置里那个「浅色主题」开关）。演示要**跟着应用走**。

    从前套件只有深色一份、这里也没有这个概念，于是切了浅色的用户：正文白底，
    一开演示就糊一块黑的（用户："色号和元件都对了，但是总体组合起来的风格还是
    不搭配啊！我们的软件是白底的，它的沙盒都是黑底的。"）。
    色号抄对只是第一步 —— **主题**也得跟上来。
    """
    try:
        from .settings_store import load as settings_load

        conf = settings_load(db) or {}
    except Exception:  # noqa: BLE001
        return "dark"  # 读不到就按默认来（`app.css` 的 `:root` 就是深色那份）
    return "light" if str(conf.get("theme") or "").strip().lower() == "light" else "dark"


def _demo_page(page: str, title: str, theme: str = "dark") -> str:
    """把模型给的 HTML 变成**带套件**的一页。

    ## 为什么由服务端注入，而不是让模型自己引

    沙箱页面原先是一张白纸：引哪个库、什么版本、怎么摆布局、用什么配色，
    全要模型每次自己决定。实测出来的结果是"能跑但难看" —— 手画的刻度是歪的、
    图例是随手贴的、每个演示一套配色、动画还常常写成 `setInterval` 改 DOM。

    所以把"公共的那一半"提到这里：**库与样式统一注入**（模型不必写任何 `<script src>`），
    模型只管写正文 —— 它写的是那个机制本身，那才是它该发挥的地方。

    ## 顺序有讲究

    经典脚本按出现顺序执行，所以：Tailwind 最先（它的预置样式要被 kit CSS 盖住）、
    React/ReactDOM 在 kit 之前、Babel 在**模型那段 `text/babel` 之前**。

    路径带 `__ORIGIN__`：沙箱里相对路径解析不了，绝对地址只能由宿主填
    （见 `pyodide_base` 的说明）。
    """
    # 主题写在 `<html>` 上：套件 CSS 的两套 token 就靠它切（`html[data-theme='light']`）。
    # **一律由这里决定** —— 模型不该、也没法知道用户此刻用的是哪一套；它自己写的
    # `data-theme` 会被抹掉（不然它会按"深色好看"自作主张，那就白底上糊黑的）。
    attr = ' data-theme="' + theme + '"'
    page = re.sub(r'\s*data-theme="[^"]*"', "", page, count=1, flags=re.I)
    if re.search(r"<html[^>]*>", page, re.I):
        page = re.sub(
            r"<html[^>]*>",
            lambda match: match.group(0)[:-1] + attr + ">",
            page,
            count=1,
            flags=re.I,
        )
    head = [
        # 公式：与正文**同一份** KaTeX（模型偶尔要在演示里写数学）。
        # 路径与 kit 一样带 `__ORIGIN__`：沙箱里相对路径解析不了。
        '<link rel="stylesheet" href="' + DEMO_ASSET_DIR + 'katex.css">',
        '<script src="' + DEMO_ASSET_DIR + 'katex.min.js"></script>',
        '<link rel="stylesheet" href="' + DEMO_KIT_DIR + 'qf-kit.css">',
    ]
    for name in DEMO_KIT_FILES:
        head.append('<script src="' + DEMO_KIT_DIR + name + '"></script>')
    block = "\n".join(head)

    if re.search(r"<head[^>]*>", page, re.I):
        return re.sub(
            r"<head[^>]*>", lambda match: match.group(0) + "\n" + block, page, count=1, flags=re.I
        )
    if re.search(r"<html[^>]*>", page, re.I):
        return re.sub(
            r"<html[^>]*>",
            lambda match: match.group(0) + "\n<head>" + block + "</head>",
            page,
            count=1,
            flags=re.I,
        )
    return (
        '<!doctype html>\n<html lang="zh"' + attr + '>\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>" + html.escape(title) + "</title>\n" + block + "\n</head>\n<body>\n"
        + page
        + "\n</body>\n</html>"
    )


def render_demo(db, args, ctx=None) -> dict:  # noqa: ANN001
    """产出一个**可运行的演示**，界面在沙箱 iframe 里跑它。

    ## 什么时候值得用

    机制里有**空间或时间上的结构**时：数据怎么流、怎么切、怎么重叠、
    流水线怎么排、哪一步是瓶颈 —— 那种东西一张能动的图胜过三段文字。

    ## 什么时候别用

    结论、定义、名词解释、代码逐行讲解。那些用文字说清更好，
    硬做个动画反而把重点冲淡，还占掉整个屏幕。

    ## 沙箱里已经给你备好了套件（别再自己引库）

    页面**自动**带上 React 18 + JSX（`<script type="text/babel">` 直接写 JSX 就行）、
    htm、d3 v7、Tailwind（配好了本项目的深色）、以及 `window.QFKit` ——
    里面是配色令牌与几个现成组件：

    * `QFKit.mount(<App/>)` —— 一行挂载（没给节点就挂到 `#app`）
    * `QFKit.Frame / Card / Row / Btn / Legend / KV / Note / Chip` —— 布局与信息块
    * `QFKit.useTicker(speed, {paused})` —— 帧驱动步进（**别用 `setInterval` 改 DOM**）
    * `QFKit.scales({width, height, xDomain, yDomain})` + `QFKit.useAxis(ref, {scales})`
      —— **坐标轴与数据共用同一组比例尺**（手写坐标轴十次九次是歪的）
    * `QFKit.colors` —— 色板（`series` 是给多条序列用的）

    ## 骨架（照它写，别从空白页开始）

        <div id="app"></div>
        <script type="text/babel">
        const { useState } = React;
        function App() {
          const [run, setRun] = useState(true);
          const t = QFKit.useTicker(1, { paused: !run });
          return (
            <QFKit.Frame title="…" caption="你在看什么：…"
              right={<QFKit.Btn on={run} onClick={() => setRun(!run)}>{run ? '暂停' : '播放'}</QFKit.Btn>}>
              <QFKit.Card title="机制">
                <svg className="qf-svg" viewBox="0 0 640 300">{/* 用 d3 算坐标，别手写字面量 */}</svg>
              </QFKit.Card>
            </QFKit.Frame>
          );
        }
        QFKit.mount(<App/>);
        </script>

    ## 其余

    * 尺寸自适应：面板宽高都会变，用 `viewBox` 与百分比，别写死像素。
    * 沙箱**允许联网**（https 的 CDN 与 fetch 都行），但套件已经够用；
      别把一切押在网络上 —— 加载失败时要还能看出个大概。
    * 拿不到网页本身的东西：沙箱不带 `allow-same-origin`，没有 cookie、
      没有 localStorage、也碰不到宿主页面的 DOM —— 数据请自己在页面里带上。
    """
    page = str(args.get("html") or "").strip()
    title = str(args.get("title") or "").strip() or "演示"
    if not page:
        return {"error": "html 不能为空。"}
    if len(page) > DEMO_MAX_CHARS:
        return {
            "error": f"演示太大了（{len(page)} 字，上限 {DEMO_MAX_CHARS}）—— 精简一版再看。",
            "note": "把动画逻辑压缩到最小可演示的程度，别把整份材料都塞进去。",
        }

    # **TikZ 不许塞进演示里** —— 这里排公式的是 KaTeX（`QFKit.Math`），它**不认 TikZ**：
    # `\begin{tikzpicture}` 进去只会显示成源码或报错。实测：用户说"再来几张 tex 图"，
    # 模型把三张 `\begin{tikzpicture}` 包进 `QFKit.Math` 交上来 —— 图**全是坏的**，
    # 而正文里明明有真 TeX 引擎可以编它们（`$$…$$`）。
    #
    # 为什么这里硬拦、不只写进提示词：这件事它已经做错过一次，而错法的代价是
    # "看着像演示、其实图是空的"。拒掉 + 说清出路，比再求一遍可靠。
    tikz = re.search(r"begin\{(tikzpicture|axis|tikzcd|circuitikz)\}", page)
    if tikz:
        return {
            "error": (
                "这份 html 里有 TikZ（`\\begin{" + tikz.group(1) + "}`），而演示里的公式是 "
                "KaTeX 排的 —— **它不认 TikZ**，放进来只会显示成源码或报错。"
                "TikZ / pgfplots / tikz-cd / circuitikz 的图请**直接写在正文里**："
                "用 `$$` 包起来（`$$\\begin{tikzpicture}…\\end{tikzpicture}$$`），"
                "正文里的图由**真的 TeX 引擎**编译，一张消息里可以放好几张。"
                "演示里真要画图就用 d3 / SVG / Canvas 自己画。"
            )
        }

    if not _demo_kit_ready():
        # 套件没同步就退回老规矩：让模型自己引库（能联网时可用）
        return {
            "demo": {"title": title[:80], "html": page},
            "note": "演示已挂上（沙箱 iframe，允许联网）。套件未就绪（`make vendor` 可取回），"
            "所以库里得自己引 —— 建议直接用 d3 这类 https CDN。",
        }

    return {
        "demo": {
            "title": title[:80],
            "html": _demo_page(page, title, _demo_theme(db)),
        },
        "note": "演示已挂在这条消息上（他那边是个沙箱 iframe，套件已自动注入）。"
        "**不要**把同一份 HTML 再贴进正文 —— 正文里说清它在演示什么、看哪里就行。",
    }


# ---------------------------------------------------------------- 图检索


# 走图默认只看**真的承载含义**的边。
#
# `contrast_with` 刻意**不在默认里**：它名义上是"易混"，实际大多是机械派生的
# —— 实测 8238 条里大量 `why` 写着「选项辨析：题 tt-arch-XXXX 把它们放在一起
# 当干扰项」，也就是"这两个概念在同一道题的选项里出现过"。拿它当语义关系，
# 邻域立刻就变成一堆随机配对（实测某个话题概念的 8 个邻居全是这种）。
#
# `co_occurs`（按字面共现）同理，甚至更弱。两者要看都得显式列进 kinds。
GRAPH_KINDS = ("requires", "part_of", "implements")

# 方向约定：`from` 是主动的那一头 —— `A →(requires) B` 读作"A 是 B 的前置"
# （库里抽读下来 6 条里 5 条这么读才通，剩一条是判边的噪声）。
# 值 = (**逆着**边走看到的, 顺着边走看到的)：
# 站在 B 上逆着走到 A，看到的 A 是「前置」；站在 A 上顺着走到 B，看到的 B 是「后继」。
_EDGE_WORDS = {
    "requires": ("前置", "后继"),
    "part_of": ("组成部分", "包含"),
    "contrast_with": ("易混", "易混"),
    "implements": ("实现", "被实现"),
    "co_occurs": ("常共现", "常共现"),
}
# 排序权重：先把前置摆上桌 —— "该先学什么"的答案在那一条上
_EDGE_ORDER = {"requires": 0, "part_of": 1, "contrast_with": 2, "implements": 3, "co_occurs": 9}


def _graph_seed(db, key: str, query: str):  # noqa: ANN001
    """线索 → 概念。key 直接命中；query 按名字片段找（取最"重"的那个）。"""
    if key:
        concept = db.scalar(select(Concept).where(Concept.key == key))
        if concept is not None:
            return concept
        point = db.scalar(
            select(KnowledgePoint)
            .where(KnowledgePoint.key == key)
            .order_by(KnowledgePoint.id)
            .limit(1)
        )
        return db.get(Concept, point.concept_id) if point is not None and point.concept_id else None

    if not query:
        return None
    like = "%" + query + "%"
    return db.scalar(
        select(Concept)
        .where(Concept.status != "retired")
        .where(
            or_(
                Concept.key.ilike(like),
                Concept.name.ilike(like),
                cast(Concept.aliases, String).ilike(like),
            )
        )
        .order_by(Concept.material_count.desc(), Concept.question_count.desc(), Concept.key)
        .limit(1)
    )


def explore_graph(db, args, ctx=None) -> dict:  # noqa: ANN001
    """从一个概念出发，走**语义关系**看邻域。

    ## 与 `search_knowledge` 的分工

    那个是"按词找节点"，这个是"从一个节点往外走"。两类问题只有它能答：

    * **我该先学什么** —— 看 `requires` 的**上游**（谁是它的前置）。
    * **这块和那块什么关系 / 为什么我学不懂它** —— 看邻域全貌与易混项。

    ## 为什么每个邻居都带掌握档位

    光说"它的前置是 A、B、C"没用 —— 有用的信息是**哪一个还没打牢**。
    档位取该概念下**最弱那个点**，并且与 `get_mastery` 共用同一份口径
    （同一件事不能有两个说法）。

    ## 默认不看 `contrast_with` 与 `co_occurs`

    前者名义是"易混"，实际大多是从**题目选项**机械派生的（"这两个概念在同一道
    题的选项里出现过"），后者是按字面共现算的。拿它们当语义关系，邻域会立刻
    变成一堆随机配对。要看就显式列进 `kinds`。

    ## 已知的缺口

    **370 / 912 个概念一条 `requires` / `part_of` 都没有** —— 也就是四成概念
    根本还没接进前置链。碰上它们时返回里会直说，而不是假装图谱很全。
    """
    key = str(args.get("key") or "").strip()
    query = str(args.get("query") or "").strip()
    depth = _clamp(args.get("depth"), 1, 2, 1)
    limit = _clamp(args.get("limit"), 1, 30, 12)
    kinds = [str(k).strip() for k in (args.get("kinds") or []) if str(k).strip()]
    kinds = [k for k in (kinds or list(GRAPH_KINDS)) if k in _EDGE_WORDS]

    seed = _graph_seed(db, key, query)
    if seed is None:
        return {"error": "没找到这个概念：给 key（概念/知识点的）或 query（名字片段）。"}

    seen: dict[int, int] = {seed.id: 0}
    frontier = [seed.id]
    reached: list[dict] = []
    for step in range(1, depth + 1):
        rows = db.execute(
            select(ConceptEdge.type, ConceptEdge.from_concept_id, ConceptEdge.to_concept_id)
            .where(ConceptEdge.type.in_(kinds))
            .where(
                or_(
                    ConceptEdge.from_concept_id.in_(frontier),
                    ConceptEdge.to_concept_id.in_(frontier),
                )
            )
        ).all()

        found = []
        for edge_type, from_id, to_id in rows:
            if from_id in frontier and to_id not in seen:
                found.append((to_id, edge_type, "out"))
            elif to_id in frontier and from_id not in seen:
                found.append((from_id, edge_type, "in"))

        if not found:
            break
        for concept_id, edge_type, direction in found:
            seen[concept_id] = step
            reached.append(
                {"id": concept_id, "type": edge_type, "direction": direction, "distance": step}
            )
        frontier = [concept_id for concept_id, _, _ in found]

    if not reached:
        return {
            "seed": {"key": seed.key, "name": seed.name},
            "neighbors": [],
            "note": "这个概念还没有语义关系边（370 / 912 个概念都还没接进前置链）。"
            "它可能只有 co_occurs / contrast_with 那种机械派生的弱边，"
            "要看就显式列进 kinds；想理解它本身，先用 search_material 读材料原文。",
        }

    ids = [item["id"] for item in reached]
    concepts = {
        concept.id: concept
        for concept in db.scalars(select(Concept).where(Concept.id.in_(ids))).all()
    }
    keys_by_concept: dict[int, list[str]] = {}
    for concept_id, point_key in db.execute(
        select(KnowledgePoint.concept_id, KnowledgePoint.key).where(
            KnowledgePoint.concept_id.in_(ids)
        )
    ).all():
        keys_by_concept.setdefault(concept_id, []).append(point_key)
    bands = _bands_by_point(
        db, [key for keys in keys_by_concept.values() for key in keys] or None
    )

    def band_of(concept_id: int) -> str:
        """该概念下**最弱**那个点的档位：说"这块还没打牢"时得用最弱的说。"""
        scored = [bands[key] for key in keys_by_concept.get(concept_id, []) if key in bands]
        if not scored:
            return "new"
        return min(scored, key=lambda item: (item["score"], item["key"]))["band"]

    items = []
    for item in reached:
        concept = concepts.get(item["id"])
        if concept is None:
            continue
        back, forward = _EDGE_WORDS.get(item["type"], ("相关", "相关"))
        items.append(
            {
                "key": concept.key,
                "name": concept.name,
                # 逆着边走（邻居是边的那一头 from）→ 用第一个词
                "relation": back if item["direction"] == "in" else forward,
                "edge": item["type"],
                "distance": item["distance"],
                "band": band_of(concept.id),
                "questions": concept.question_count,
                "definition": (concept.definition or "")[:120],
            }
        )
    items.sort(key=lambda item: (_EDGE_ORDER.get(item["edge"], 9), item["distance"], item["key"]))

    return {
        "seed": {"key": seed.key, "name": seed.name, "band": band_of(seed.id)},
        "neighbors": items[:limit],
        "note": "relation 是从 seed 出发读的（前置 = 学它之前得先会；后继 = 以它为前提）；"
        "band 是该概念下最弱那个点的掌握档位（与 get_mastery 同一口径）。",
    }


# ---------------------------------------------------------------- 材料正文


def _query_vector(db, query: str):  # noqa: ANN001
    """把查询句子变成向量。**拿不到就返回 None** —— 检索层会退回纯字面并说明。

    三条护栏，都是为了"**别让一次向量化失败拖垮整次检索**"：

    * 库里一片向量都没有（还没跑 `python -m pipeline.embed`）→ **连试都不试**。
      试了也没有可比的东西，纯白花一次调用。
    * 算不出查询向量（模型没就绪 / 通道不给 embedding）→ 返回 None，
      字面那一路照常出结果。
    * **没配密钥 / AI 被关掉**（工具表允许脱离请求直接调，测试里就是这么调的）
      → 同样返回 None。这条不能漏：`resolve_config` 在那种情况下抛 503，
      漏了它会让"纯字面检索"这条本应可用的路整条炸掉。

    两条路，**本地优先**：本地小模型离线可用、零边际成本，而这是每次检索都要走的路
    （远程按次计费是随使用量长出来的，见 `local_embed` 的模块注释）。
    本地没就绪才退到远程通道。
    """
    text = str(query or "").strip()
    if not text:
        return None
    # 库里一片向量都没有：向量算得再准也没有东西可比
    if not semantic.state(db).get("ready"):
        return None

    from . import local_embed  # 延迟导入：它要读设置，且要等模型下载完

    if local_embed.is_ready():
        try:
            vectors = local_embed.embed([text], kind="query")
        except (RuntimeError, ValueError, OSError, ImportError):
            # 模型坏了（文件被删 / onnx 加载失败）不该让整次检索挂掉
            vectors = []
        if vectors:
            return vectors[0]

    from fastapi import HTTPException

    from . import ai_gateway as gateway  # 延迟导入：与 web_search 同一条理由

    try:
        conf = gateway.resolve_config(db)
    except HTTPException:
        return None
    try:
        vectors = gateway.embed_texts(conf, [text])
    except (gateway.EmbeddingUnavailable, gateway.UpstreamError, httpx.HTTPError):
        return None
    return vectors[0] if vectors else None


def search_material(db, args, ctx=None) -> dict:  # noqa: ANN001
    """在材料**原文**里检索 —— 不是概念库，是正文本身。

    ## 两条路一起用，`via` 会告诉你哪条找到的

    * **字面**：你给的原词在原文里出现过 —— 精确、可解释，命中哪几行说得清；
    * **向量**：**你换个说法问同一件事**（「上三角屏蔽」↔ `causal mask`）——
      它按"意思"找，不要求词一样。

    两路结果用 RRF 融合，**被两路都找到的排在只被一路找到的前面**。

    ## 三条使用规则（也是给模型的）

    * 词给**特别的**：`exp_approx_mode`、`circular buffer` 这种原词最灵；
      给"地址""性能"这种到处都有的词，等于没筛。
    * 字面那一路要求所有词在**同一行**同时出现才算命中。放宽就少给一个词。
    * 返回里带 `scanned` / `total`：没扫完就是没扫完，别当成"全库没有"。
      **`semantic` 为 false** 时说明这次只有字面参与了（通道没配好）——
      别把"它接不住同义改写"当成"材料里没有这件事"。

    ## 与 `search_knowledge` 的分工

    `search_knowledge` 查的是**知识空间**（概念、点、题）；这个查的是**文本**。
    想知道"材料里原话怎么说的"，用这个；想知道"这个点在图谱里的位置"，用那个。
    """
    return _search_by_depth(db, args, "出题")


def _search_by_depth(db, args, depth: str) -> dict:  # noqa: ANN001
    """两条检索工具共用的身子：只有"看哪一档资料"不同。

    `sources` 是给宿主看的约定：router 会把它变成**引用零件**，
    于是对话里每一处结论都能点回原文那几行（不只是"我看过材料"）。
    其他工具（`search_knowledge` / `get_point_detail`）也照这个字段名返回。
    """
    query = str(args.get("query") or "")
    result = semantic.search_fused(
        db,
        query,
        query_vec=_query_vector(db, query),
        slug=str(args.get("slug") or "").strip(),
        limit=_clamp(args.get("limit"), 1, 8, 5),
        depth=depth,
    )
    hits = result.get("hits") or []
    if hits:
        result["sources"] = [
            {
                "material": hit["material"],
                "title": hit["title"],
                "startLine": hit["firstMatch"],
                "endLine": hit["endLine"],
                "quote": hit.get("quote") or "",
            }
            for hit in hits
        ]
    # **每次都报一句"这一层是哪个书架"。**
    #
    # 我原先写的是"没命中时才提示"，那是个错前提：向量那一路总会给点东西，命中**从来不空**
    #（实测：拿一串绝不存在的字符去问，照样回来 5 条）。于是那句提示一次都没出现过 ——
    # 而"在一个书架里没找到就下结论"正是这里最容易犯的错（我自己就犯过：拿
    # `search_material` 问"二分查找"，回来的全是出题层那 60 份 tt-metal，于是以为库里没有，
    # 而答案就在检索层的 hello-algo 里）。
    #
    # 所以不赌命中数，改成无条件说清：这次查的是哪一层、另一层叫什么、去哪儿接着问。
    result["shelf"] = {
        "used": depth,
        "other": "检索" if depth == "出题" else "出题",
        "note": (
            "这次查的是「%s」层。资料分两个书架：出题层（我在学的）与检索层（我查的），"
            "检索方式一样、只差一个档位 —— 在这里没有满意的答案时，换 %s 再问一次，"
            "别据此断定「材料里没有」。" % (depth, "search_library" if depth == "出题" else "search_material")
        ),
    }
    return result


def search_library(db, args, ctx=None) -> dict:  # noqa: ANN001
    """在**资料库**里检索（`depth=检索` 的那一档）—— 和 `search_material` 是一对。

    ## 和 `search_material` 的分工，只看一件事：**这份资料要不要出题**

    * `search_material` → `depth=出题`：**我在学的**（值得精读、抽知识点、进图谱、
      出成题的语料）。技术报告、手册这一档。
    * `search_library` → `depth=检索`：**我查的**（拿来读、拿来引用、拿来对照，
      但"灌水论文我也要出成题目做吗"）。论文、白皮书、别人的资料这一档。

    两边的正文与检索方式**完全一样**（都是切片 + 向量 + 字面 RRF 融合，
    都能点回原文行区间），**差别只有这一个档位**。

    ## 一条使用规矩

    **在一个里没找到，就去另一个里再问一次** —— 它们不是"同一个库的两种搜法"，
    而是两个书架。只搜一个就下结论（"材料里没有"），是这里最容易犯的错。
    """
    return _search_by_depth(db, args, "检索")


def read_material(db, args, ctx=None) -> dict:  # noqa: ANN001
    """按 (slug, 行区间) 读材料原文。

    这是"引用能点回原文"的服务端一半：前端点开引用时读的是同一个函数，
    所以模型看到的那几行与用户看到的那几行**逐字一致**。
    默认读 60 行；想接着往下看就把 startLine 往后挪。
    """
    slug = str(args.get("slug") or "").strip()
    if not slug:
        return {"error": "要给我材料的 slug（`search_material` / `search_knowledge` 的结果里都有）。"}

    material = db.scalar(select(Material).where(Material.slug == slug))
    if material is None:
        return {"error": "没有这份材料：" + slug}

    try:
        lines = materials.read_lines(material, args.get("startLine") or 1, args.get("endLine") or 0)
    except materials.MaterialError as exc:
        return {"error": str(exc)}

    first, last = lines[0]["line"], lines[-1]["line"]
    return {
        "material": material.slug,
        "title": material.title,
        "startLine": first,
        "endLine": last,
        "lines": len(lines),
        "text": "\n".join(f"{item['line']}: {item['text']}" for item in lines),
        "sources": [
            {
                "material": material.slug,
                "title": material.title,
                "startLine": first,
                "endLine": last,
            }
        ],
    }


# ---------------------------------------------------------------- 大题批改写回


def grade_problem(db, args, ctx=None) -> dict:  # noqa: ANN001
    """把一次**大题批改**的结果写进答题记录。

    这是这一层里唯一会改状态的工具，而它改的是 `records` —— 与选择题/填空题
    同一条路。于是掌握度、间隔重复、错题本**不必为大题再立一套**：记录是事实，
    其余都是它的投影（前端按同一套规则算）。

    `verdicts` 是逐问的判定：`[{index, verdict, score, comment}]`，
    verdict 取 `correct` / `partial` / `wrong`。整体判定取各问的均值：
    全都对 → correct；一点没对 → wrong；其余 → partial。
    """
    question_id = str(args.get("questionId") or "").strip()
    question = db.get(Question, question_id) if question_id else None
    if question is None or question.retired_at is not None:
        return {"error": "题库里没有这道题：" + (question_id or "(空)")}

    raw = args.get("verdicts")
    if not isinstance(raw, list) or not raw:
        return {"error": "verdicts 不能为空（逐问给 [{index, verdict, score, comment}]）。"}

    verdicts = []
    for item in raw[:12]:
        if not isinstance(item, dict):
            continue
        verdict = str(item.get("verdict") or "").strip().lower()
        if verdict not in ("correct", "partial", "wrong"):
            verdict = "partial"
        try:
            score = float(item.get("score"))
        except (TypeError, ValueError):
            score = {"correct": 1.0, "partial": 0.5, "wrong": 0.0}[verdict]
        verdicts.append(
            {
                "index": int(item.get("index") or len(verdicts) + 1),
                "verdict": verdict,
                "score": max(0.0, min(1.0, score)),
                "comment": str(item.get("comment") or "")[:600],
            }
        )
    if not verdicts:
        return {"error": "verdicts 里没有一条能用的条目。"}

    score = sum(item["score"] for item in verdicts) / len(verdicts)
    if all(item["verdict"] == "correct" for item in verdicts):
        status = "correct"
    elif all(item["verdict"] == "wrong" for item in verdicts):
        status = "wrong"
    else:
        status = "partial"

    now = int(time.time() * 1000)
    record = db.scalar(select(Record).where(Record.question_id == question.id))
    if record is None:
        record = Record(
            question_id=question.id,
            attempts=0,
            correct=0,
            wrong=0,
            mastered=False,
            flagged=False,
            partial=0,
            first_at=now,
            patch={},
            client_rev=0,
        )
        db.add(record)

    record.attempts = (record.attempts or 0) + 1
    record.correct = (record.correct or 0) + (1 if status == "correct" else 0)
    record.wrong = (record.wrong or 0) + (1 if status == "wrong" else 0)
    record.partial = (record.partial or 0) + (1 if status == "partial" else 0)
    record.last_at = now
    record.last_status = status
    record.last_score = round(score, 3)
    record.last_response = (
        "大题（" + str(len(verdicts)) + " 问）："
        + "、".join(f"第{item['index']}问 {item['verdict']}" for item in verdicts)
    )
    # JSON 列：造新 dict 再挂回去（原地改不会触发脏检查，实测静默丢过）
    patch = dict(record.patch or {})
    history = list(patch.get("problem") or [])[-19:]
    history.append({"at": now, "score": round(score, 3), "verdicts": verdicts})
    patch["problem"] = history
    record.patch = patch
    flag_modified(record, "patch")

    db.commit()
    return {
        "recorded": True,
        "status": status,
        "score": round(score, 3),
        "attempts": record.attempts,
        "note": "已写进答题记录（掌握度与间隔重复跟着它走）。"
        "现在开始逐问讲评：先说他答对的，再指出缺口与依据。",
    }


# ---------------------------------------------------------------- 动作提案


# AI 能提的写操作，只有这两件 —— 而且都是**界面里本来就有的动作**
# （刷题页的收藏夹、错题本里的「已掌握」）。它自己不改状态：这里只产出一张
# 凭条，人点了才生效。所以不需要另搞一套审批机制 ——
# 需要确认的那一步，就是用户自己那一次点击。
#
# 刻意**没有**的东西：改掌握度（掌握度只能从作答长出来）、
# 改 SM2 排期（那是"答得怎么样"的产物）、改题库（那要走有出处的流水线）。
_ACTION_FIELDS = {"flag": "flagged", "mastered": "mastered"}


def _question_proposal(db, question_id, kind: str, on) -> dict:  # noqa: ANN001
    """组装一张凭条。**只读不写** —— 这是它存在的全部意义。"""
    question_id = str(question_id or "").strip()
    if not question_id:
        return {"error": "要告诉我哪道题（questionId）。"}
    question = db.get(Question, question_id)
    if question is None or question.retired_at is not None:
        return {"error": "题库里没有这道题：" + question_id}

    field = _ACTION_FIELDS[kind]
    record = db.scalar(select(Record).where(Record.question_id == question_id))
    current = bool(getattr(record, field, False)) if record is not None else False

    return {
        "proposal": {
            "kind": kind,
            "questionId": question_id,
            "on": bool(on),
            "current": current,
            "layer": question.layer,
            "wing": question.wing,
            "topic": question.topic,
        },
        "note": "界面上已经放了一张**待确认**的凭条，他点了才生效。"
        "所以别说「我已经帮你收藏了」，要说「要不要把它收起来」。",
    }


def flag_question(db, args, ctx=None) -> dict:  # noqa: ANN001
    """提案：把某道题加入收藏夹 / 移出收藏夹。**不改任何东西**。

    ## 为什么是提案而不是直接写

    它写的是**用户的学习记录**，那是他的东西。AI 可以建议"这题值得留着"，
    但按下去的那一下得是他自己 —— 界面上会出现一张凭条，
    他点确认才落到记录里（走的是收藏夹那条老路）。
    """
    return _question_proposal(db, args.get("questionId"), "flag", args.get("on", True))


def mark_mastered(db, args, ctx=None) -> dict:  # noqa: ANN001
    """提案：把某道题标成「已掌握」（错题本不再催它）/ 取消这个标记。**不改任何东西**。

    这道题的作答记录**不会**因此变化 —— 标记掌握是"别再催我了"，
    不是"我答对了"。掌握度仍然只从作答长出来。
    """
    return _question_proposal(db, args.get("questionId"), "mastered", args.get("on", True))


# ---------------------------------------------------------------- 登记


DRAFT_TYPES = ("single", "multi", "blank", "short", "problem")


def _draft_options(raw: object) -> list[dict]:
    """选项：既接受 `[{key,text}]`，也接受 `["文字", …]`（模型两种都会写）。"""
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    for index, item in enumerate(raw):
        letter = chr(ord("A") + index)
        if isinstance(item, dict):
            text = str(item.get("text") or item.get("label") or "").strip()
            key = str(item.get("key") or letter).strip().upper()[:2] or letter
        else:
            text = str(item or "").strip()
            key = letter
        if text:
            out.append({"key": key, "text": text})
    return out


def create_question(db, args, ctx=None) -> dict:  # noqa: ANN001
    """自己出一道题 —— **临时题**：先只留在对话里，用户按「存进题单」才落库。

    ## 为什么要这个工具

    题库是按知识点预先出好的（`push_question` 从里面挑）。但真实的教学里有一半是
    "就着刚才这段话，我给你编一道" —— 材料里没有现成的、或用户想要个更贴他错法的。
    没有这个工具时，模型只能推题库里的题，于是常常"讲得很好、一到练就离题"。

    ## 临时题与题库的关系

    * **默认不落库**：返回的是一张草稿卡，用户看着它作答；他觉得值得留，按「存进题单」
      才写进 `my_questions`（与公共题库分开，见 `routers/mybank.py`）。
    * **随出随改**：要改就再调一次这个工具（或让用户说哪里不对）—— 每次都是新草稿，
      不必去动题单里已有的那道。
    * 答案**随草稿一起发给界面**（前端要判分），但**不进模型的上下文回放**
      （见 `output_text` 对 `draft` 的裁剪）。

    ## 写题的要求

    题干要**自足**（不依赖刚讲过的原话）、选项要互相排斥、错误项要有诊断价值
    （每个错项对应一种典型误解，写进 `explanation`）。解析里说清"错的人是怎么想的"——
    这比"正确答案是 B"有用得多。
    """
    raw = args.get("question")
    if not isinstance(raw, dict):
        return {"error": "要给出 question（题目本身）。"}

    stem = str(raw.get("stem") or "").strip()
    if not stem:
        return {"error": "题干不能为空。"}

    qtype = str(raw.get("type") or "single").strip().lower()
    if qtype not in DRAFT_TYPES:
        return {"error": f"type 得是这几种之一：{'/'.join(DRAFT_TYPES)}。"}

    draft: dict = {
        "id": "draft-" + uuid.uuid4().hex[:8],
        "type": qtype,
        "stem": stem,
        "layer": str(raw.get("layer") or "").strip()[:8],
        "wing": str(raw.get("wing") or "").strip()[:8],
        "topic": str(raw.get("topic") or "").strip()[:64],
        "pointKey": str(args.get("pointKey") or raw.get("pointKey") or "").strip()[:96],
        "difficulty": int(raw.get("difficulty") or 3) if str(raw.get("difficulty") or "3").isdigit() else 3,
        "explanation": str(raw.get("explanation") or "").strip(),
        "hint": str(raw.get("hint") or "").strip(),
    }

    if qtype in ("single", "multi"):
        options = _draft_options(raw.get("options"))
        if len(options) < 2:
            return {"error": "选择题至少要两个选项（options）。"}
        answer = raw.get("answer")
        if isinstance(answer, list):
            answer = ",".join(str(item).strip() for item in answer if str(item).strip())
        answer = str(answer or "").strip().upper()
        keys = {item["key"] for item in options}
        chosen = [part.strip() for part in answer.replace("，", ",").split(",") if part.strip()]
        if not chosen or not set(chosen) <= keys:
            return {"error": "answer 要是选项里的字母（多选写成 \"A,C\"）。"}
        draft["options"] = options
        draft["answer"] = ",".join(chosen)
    elif qtype == "blank":
        answer = str(raw.get("answer") or "").strip()
        if not answer:
            return {"error": "填空题得有 answer。"}
        draft["answer"] = answer
        accepts = raw.get("accepts")
        if isinstance(accepts, list):
            draft["accepts"] = [str(item).strip() for item in accepts if str(item).strip()]
    elif qtype == "problem":
        subs = raw.get("questions")
        if not isinstance(subs, list) or not subs:
            return {"error": "大题要给出 questions（小问列表）。"}
        cleaned = []
        for index, sub in enumerate(subs, start=1):
            if not isinstance(sub, dict):
                continue
            text = str(sub.get("stem") or "").strip()
            if not text:
                continue
            cleaned.append(
                {
                    "index": int(sub.get("index") or index),
                    "title": str(sub.get("title") or f"第 {index} 问").strip()[:80],
                    "stem": text,
                    "hint": str(sub.get("hint") or "").strip(),
                    "reference": str(sub.get("reference") or "").strip(),
                    "points": [str(item).strip() for item in (sub.get("points") or []) if str(item).strip()]
                    if isinstance(sub.get("points"), list)
                    else [],
                }
            )
        if not cleaned:
            return {"error": "大题的小问都得有题干（stem）。"}
        draft["questions"] = cleaned
    else:  # short
        answer = str(raw.get("answer") or raw.get("reference") or "").strip()
        if not answer:
            return {"error": "简答题得有 answer（参考答案）。"}
        draft["answer"] = answer
        rubric = raw.get("rubric")
        if isinstance(rubric, list):
            draft["rubric"] = [str(item).strip() for item in rubric if str(item).strip()]

    return {
        "draft": draft,
        "note": "题目已挂在这条消息上（他那边是**临时题卡**：能直接作答，默认不进题单）。"
        "**不要**把题干与选项再贴进正文 —— 正文里说清这道题在考什么、以及它对着哪个知识点就好。"
        "他说要存进题单时，界面上的按钮会处理；他说题目哪里不对时，**改一版重新调这个工具**。",
    }


# ---------------------------------------------------------------- 笔记 / 资料（可挂载）


def read_note(db, args, ctx=None) -> dict:  # noqa: ANN001
    """读一篇笔记：正文 + 大纲 + 反链。

    与 `search_notes` 的分工：那个回答"命中在哪"，这个回答"那一篇写了什么"。
    这一步原先**是断的** —— `search_notes` 的说明里写着"要读全文，让用户打开它"，
    等于模型搜到了却读不到（用户："chat 这边还没接上笔记库"）。

    与资料侧的 `read_material` 对齐：那边按**行区间**读原文，这边按**字区间**读正文
    （笔记没有行号那套东西，但"一次读不完要能接着读"是同一个要求 —— 见 `startChar`）。
    大纲与反链一起带上：模型常常正是为了"这篇和别的什么有关"才要读它。
    """
    from . import notelib  # noqa: PLC0415

    rel = str(args.get("path") or "").strip()
    if not rel:
        return {"error": "要给我笔记的相对路径（`search_notes` / `list_notes` 的结果里都有）。"}
    lib_name = str(args.get("lib") or "").strip()
    try:
        # 走共用的那个：它把"`lib` 可省"变成真的（原来 `library("")` 会抛
        # "没有这个库"，于是这里声明可省、实际不传就报错）。
        note = notelib.read_note(_note_library(args), rel)
    except notelib.NoteNotFound as exc:
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"读不到：{exc}"}

    body = str(note.get("body") or "")
    #: 这一次最多给多少字。**默认 12000 → 40000**：12000 那会儿连一篇 4.5 万字的
    #: 笔记都读不完（库里实测就有），模型只能看到开头。
    limit = _clamp(args.get("maxChars"), 800, 100000, 40000)
    #: 从第几个字开始读。**这个参数是在补一个断掉的约定**：原来的提示语写着
    #: "想接着看就说清要看哪一段"，可那时**根本没有任何参数能取到后半段** ——
    #: 一篇 13.7 万字的笔记，永远只有前 40000 字是可见的，剩下的一直够不着。
    #: 资料侧早就不是这样：`read_material` 按**行区间**读，"想接着往下看就把
    #: startLine 往后挪"。笔记这边缺的是同一个东西，只是单位是字、不是行。
    start = _clamp(args.get("startChar"), 0, len(body), 0)
    chunk = body[start:start + limit]
    #: 下一次该从哪儿接着读；`None` = 已经到结尾了
    tail = start + len(chunk) if start + len(chunk) < len(body) else None
    out: dict = {
        "lib": note.get("lib") or lib_name,
        "path": note.get("path") or rel,
        "title": note.get("title") or "",
        "kind": note.get("kind") or "note",
        "chars": len(body),
        "startChar": start,
        "truncated": tail is not None,
        "text": chunk,
        "note": (
            "这是全文。"
            if tail is None and not start
            else "给的是第 %d–%d 字（原文 %d 字）。%s"
            % (
                start + 1,
                start + len(chunk),
                len(body),
                "后面还有 —— 要接着读就再调一次这个工具、传 startChar=%d。" % tail
                if tail is not None
                else "已经到结尾了。",
            )
        ),
    }
    if note.get("outline"):
        # **只带前若干项**：实测一篇 4.5 万字的笔记有 837 条大纲 —— 全塞进
        # 上下文纯烧 token。要看细的，让它按需再读一次（或者问用户看哪一节）。
        out["outline"] = note["outline"][:40]
    if note.get("backlinks"):
        out["backlinks"] = note["backlinks"][:20]
    return out


def list_notes(db, args, ctx=None) -> dict:  # noqa: ANN001
    """列出笔记库；带 `lib` 时给那个库的笔记清单。

    为什么需要：`search_notes` 是**按关键词**找 —— 用户问"我笔记里都记了什么"时，
    模型手里没有"有几个库、各有什么"这张图，只能空搜或者干脆说不知道。
    这个工具给那张图。
    """
    from . import notelib  # noqa: PLC0415

    lib_name = str(args.get("lib") or "").strip()
    if not lib_name:
        try:
            libs = [
                {"lib": lib.name, "notes": lib.notes, "canvases": lib.canvases, "external": lib.external}
                for lib in notelib.libraries()
            ]
        except Exception as exc:  # noqa: BLE001
            return {"error": f"列不出来：{exc}"}
        return {
            "libraries": libs,
            "note": "要看某个库里有哪几篇，带 `lib` 再调一次；读某一篇用 `read_note`。",
        }

    try:
        tree_data = notelib.tree(notelib.library(lib_name))
    except Exception as exc:  # noqa: BLE001
        return {"error": f"列不出来：{exc}"}

    # 树是嵌套的（dirs / files）—— 压成"路径清单"给模型更好用：它要的是
    # "有哪些篇、怎么称呼它们"，而不是给前端渲染的那棵带缩进的树。
    found: list[str] = []

    def walk(node: dict) -> None:
        for item in node.get("files") or []:
            rel = str(item.get("path") or "")
            if not rel:
                continue
            title = str(item.get("title") or "")
            found.append(f"{rel}｜{title}" if title else rel)
        # 注：`tree()` 出来的是 **finalize 之后**的形状 —— `dirs` 已被它从
        # dict 摊成 list（每个子节点自己带着相对路径）。第一版我按 dict 遍历，
        # 对 list 调 `.items()` 抛错、又被下面的 try 吞掉，于是**只列出了根层**
        #（实测：`libraries()` 说 96 篇，这里只给 10 篇）。两种形状都认。
        dirs = node.get("dirs") or []
        if isinstance(dirs, dict):
            dirs = list(dirs.values())
        for child in dirs:
            if isinstance(child, dict):
                walk(child)

    try:
        walk(tree_data)
    except Exception:  # noqa: BLE001
        pass
    limit = _clamp(args.get("limit"), 1, 500, 200)
    return {
        "lib": lib_name,
        "count": len(found),
        "notes": found[:limit],
        "note": "每条是「相对路径｜标题」；读全文用 `read_note`。"
        + (f"（只给了前 {limit} 条，共 {len(found)} 条）" if len(found) > limit else ""),
    }


def search_notes(db, args, ctx=None) -> dict:  # noqa: ANN001
    """在笔记里找。**跨所有笔记库**（Math / Personal / Philosophy / Tech）。

    与 `search_material` 的分工：那份材料在**资料根**下（PDF、手册、别人的东西），
    这里的笔记是**用户自己写的**。两边的检索表达式是同一套。
    """
    from . import notelib  # noqa: PLC0415

    query = str(args.get("query") or "").strip()
    if not query:
        return {"error": "得给一个 query"}
    limit = _clamp(args.get("limit"), 1, 30, 12)
    lib_name = str(args.get("lib") or "").strip()
    try:
        hits = notelib.search(query, lib_name=lib_name, limit=limit)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"检索失败：{exc}"}
    return {
        "items": hits,
        "count": len(hits),
        "note": "命中含库名与路径；**要读某一篇的全文就调 `read_note`**（别让用户自己去打开）。",
    }


def _note_library(args):  # noqa: ANN001
    """按 `lib` 找笔记库；**省了就取第一个**。

    三个笔记工具共用这一段。抽出来是因为原来是**两份**，且行为不一致：
    `read_note` 直接 `notelib.library(name)`，而 `library("")` 会抛"没有这个库" ——
    于是它工具说明里写的"`lib` 可省"**是假的**（不传就报错）；`write_note` 则自己
    写了一遍"没给就取第一个"。同一件事写两处，就会有一处是错的。
    """
    from . import notelib  # noqa: PLC0415

    name = str(args.get("lib") or "").strip()
    if name:
        return notelib.library(name)
    found = notelib.libraries()
    if not found:
        raise notelib.NoteNotFound("还没有任何笔记库")
    return found[0]


def _excerpt(text: str, start: int, length: int, pad: int = 60) -> str:
    """给出 `text` 里 `[start, start+length)` 那一段，前后各带一点上下文。

    干什么用：改完之后**把落下去的那一段念一遍** —— 模型据此能自己核对"改的是不是
    我想改的地方"，比一句"已修改"有用得多（笔记是**用户的文件**，改错地方代价很大）。
    """
    lo = max(0, start - pad)
    hi = min(len(text), start + length + pad)
    return ("…" if lo > 0 else "") + text[lo:hi] + ("…" if hi < len(text) else "")


def edit_note(db, args, ctx=None) -> dict:  # noqa: ANN001
    """改一篇笔记里的**某一处**：给一段原文（`old`），换成新的（`new`）。

    与 `write_note` 的分工：那个只在**末尾**接一段；这个能改中间、能替换，也能删
    （`new` 给空串）。

    **为什么非要"给一段原文"，而不是行号或"第几小节"**：行号会随上一次写入漂移；
    "第三小节"要靠自然语言猜，而小节名在长笔记里很容易重复。两者都会**改错地方
    而且不报错**。给一段逐字原文，找不到就当场拒绝 —— 这是唯一一种"错了会自己喊"
    的改法。所以 `old` 必须从 `read_note` 的结果里**逐字**复制（空白、换行、
    全角半角都算差异）。

    **同名段落的坑**：一段话出现两次以上（两个小节写着同一句很常见），默认
    **拒绝**并列出它在哪几处 —— 多带一行上下文再试，或者传 `all=true` 明确表示
    "这些地方都要改"。**不设"只改第一处"这种默认**：那是最容易悄悄改错的一种设计。

    写入走的是**笔记页保存的同一条路**（`notelib.write_body`）：YAML 头原样保留、
    改前自动留快照（能撤回）。元数据头（标题/标签）不归它管 —— 那是 `set_meta`。
    """
    from . import notelib  # noqa: PLC0415

    rel = str(args.get("path") or "").strip()
    if not rel:
        return {"error": "得给 path（笔记在库里的相对路径）"}
    old = args.get("old")
    if not isinstance(old, str) or not old:
        return {"error": "得给 old：要被替换掉的那段原文（从 `read_note` 的结果里逐字复制）"}
    if args.get("new") is None:
        return {"error": '得给 new（换成什么）。想**删掉**这一段就给个空串：""'}
    new = args.get("new")
    if not isinstance(new, str):
        return {"error": 'new 得是字符串；想删掉这一段就给个空串：""'}
    if old == new:
        return {"error": "old 与 new 一模一样，等于没改"}

    try:
        target = _note_library(args)
        note = notelib.read_note(target, rel)
    except notelib.NoteError as exc:
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"读不到：{exc}"}

    body = str(note.get("body") or "")
    hits: list[int] = []
    at = body.find(old)
    while at >= 0 and len(hits) < 20:
        hits.append(at)
        at = body.find(old, at + len(old))
    if not hits:
        return {
            "error": "正文里找不到这段原文（一个字符都没匹配上）。最可能是没逐字复制 —— "
            "空白、换行、全角半角都算差异。**先 `read_note` 把那段读回来、原样复制**"
            "（长笔记还要用 startChar 读到那一段所在的位置：没读到的部分当然也匹配不上）。"
        }
    if len(hits) > 1 and not args.get("all"):
        return {
            "error": "这段原文在正文里出现了 %d 次，**我不知道你要改哪一处**。"
            "把上下文多带一点（前后各带一行）再试；如果这些地方都要改，传 all=true。" % len(hits),
            "occurrences": [
                {"at": one, "context": _excerpt(body, one, len(old), pad=40)}
                for one in hits[:5]
            ],
        }

    merged = body.replace(old, new) if args.get("all") else body.replace(old, new, 1)
    try:
        notelib.write_body(target, rel, merged, why="ai_edit")
    except Exception as exc:  # noqa: BLE001
        return {"error": f"没写成：{exc}"}
    return {
        "ok": True,
        "lib": target.name,
        "path": rel,
        "replaced": len(hits) if args.get("all") else 1,
        "chars": {"before": len(body), "after": len(merged)},
        # 把**落下去的那一段**念一遍：模型据此能自己核对改的位置对不对
        "after": _excerpt(merged, hits[0], len(new)),
        "note": "已改。改动前的版本留在快照里，笔记页可以撤回。",
    }


def write_note(db, args, ctx=None) -> dict:  # noqa: ANN001
    """把一段话**追加到某篇笔记的末尾**。

    **只在"接到末尾"时用它**（用户说"记到那篇笔记里""把这段结论挂上去"）。
    要改已有的某一段、删某一句、替换某个词，用 `edit_note` —— 拿这个去改中间
    是做不到的，它只会把新内容堆到文末。

    写入前会留一份快照，所以"写错了"随时能撤回（与笔记页那个撤销同源）。
    只写**正文**，不动元数据头 —— 头是笔记的属性，不该被一次追加顺手改掉。
    """
    from . import notelib  # noqa: PLC0415

    path = str(args.get("path") or "").strip()
    text = str(args.get("text") or "").rstrip()
    if not path:
        return {"error": "得给 path（笔记在库里的相对路径）"}
    if not text:
        return {"error": "得给 text（要写进去的内容）"}
    try:
        target = _note_library(args)
        note = notelib.read_note(target, path)
        body = str(note.get("body") or "").rstrip()
        merged = (body + "\n\n" + text).strip() + "\n"
        notelib.write_body(target, path, merged, why="ai_append")
    except Exception as exc:  # noqa: BLE001
        return {"error": f"没写成：{exc}"}
    return {
        "ok": True,
        "lib": target.name,
        "path": path,
        "addedChars": len(text),
        "note": "已追加。改动前的版本留在快照里，笔记页可以撤回。",
    }


def attach_material(db, args, ctx=None) -> dict:  # noqa: ANN001
    """把一份资料挂进这次对话：给出它的元数据与一段正文。

    "挂"的实际含义是**把正文交给这一轮的模型上下文**（条目粒度是计划里定死的：
    一个条目 = 主文件 + 它引用的附属资源）。正文没抽过就顺手抽一次；
    抽出来是乱码也照实说 —— 那种情况（实测 `Trefethen-Bau.pdf`）让模型别硬引。
    """
    from . import attachments as attachments_mod  # noqa: PLC0415
    from . import library as lib  # noqa: PLC0415
    from .routers.library import _meta_dir, _text_dir, roots_for  # noqa: PLC0415

    want = str(args.get("citekey") or args.get("name") or "").strip()
    if not want:
        return {"error": "得给 citekey 或文件名"}
    chars = _clamp(args.get("chars"), 500, 20000, 6000)
    roots = roots_for(None)
    meta_dir, text_dir = _meta_dir(), _text_dir()
    found = lib.entries(roots, meta_dir, text_dir)
    lowered = want.lower()
    hit = next((one for one in found if one.citekey == want), None) or next(
        (
            one
            for one in found
            if lowered in one.item.rel.lower() or lowered in str(one.meta.get("title") or "").lower()
        ),
        None,
    )
    if hit is None:
        return {"error": f"资料库里没有这一份：{want}", "hint": "先 search_material 找一下它的引用键"}
    key = hit.citekey
    if not lib.text_state(text_dir, key).get("at"):
        lib.extract_text(hit.item, text_dir, key)
    state = lib.text_state(text_dir, key)
    return {
        "citekey": key,
        "title": str(hit.meta.get("title") or ""),
        "authors": list(hit.meta.get("authors") or []),
        "year": hit.meta.get("year") or 0,
        "kind": str(hit.meta.get("kind") or ""),
        "source": str(hit.item.path),
        "textState": str(state.get("state") or "none"),
        "text": lib.text_of(text_dir, key, limit=chars),
        # 抽不出文字时说清缺哪个组件 —— 不然用户只会觉得"这份文件坏了"
        "capabilities": (
            [
                {"缺失": one["binary"], "用途": one["why"]}
                for one in attachments_mod.capabilities()["components"]
                if not one["available"]
            ]
            if not text
            else []
        ),
        "note": "text 是这份资料的正文节选。引用时请说清是**哪一份的哪一段**；"
        "textState 不是 ok 时（garbled）说明这份抽不出可用文字，别拿它当依据。",
    }


# ---------------------------------------------------------------- 联网
#
# 前面几路检索全在**本机**（笔记 / 资料 / 图谱 / 题库），问到"外面的事"它们都答不了。
# 这一组补那个缺口：`web_search` 搜、`read_web_page` 读正文（实现见 app/websearch.py）。
#
# 两个都放在同一个 `try` 的形状里：**配置没配好不抛**，交回一条说清"去哪儿填"的消息 ——
# 模型会念给用户听，那比一句"搜索失败"有用得多（本模块 docstring 那条"异常边界"）。


def web_search(db, args, ctx=None) -> dict:  # noqa: ANN001
    """搜一次公开网页。"""
    from . import websearch  # 延迟导入：它要读设置，而设置在导入期还没就位

    query = str(args.get("query") or "").strip()
    if not query:
        return {"error": "要搜什么？`query` 是空的。"}
    stats: dict = {}
    try:
        conf = websearch.resolve(db)
        items = websearch.search(
            conf,
            query,
            count=args.get("count"),
            include_domains=args.get("includeDomains"),
            exclude_domains=args.get("excludeDomains"),
            stats=stats,
        )
    except websearch.SearchError as exc:
        return {"error": str(exc)}
    if not items and stats.get("dropped"):
        # **筛空 ≠ 没搜到** —— 这两个的下一步动作完全不同（一个是去掉域名限制，
        # 一个是换词或者承认查不到）。实测踩到过：限定 arxiv/ACM/IEEE/Springer
        # 拿回"零结果"，而实际是那 10 条里**一条都不在**这些站上，
        # 模型于是得出了"这世上没有"的结论。这条 note 就是拦这个的。
        return {
            "query": query,
            "results": [],
            "note": "**不是没搜到，是你给的域名限制把结果全筛掉了**：这次拿回 "
            + str(stats.get("raw") or 0)
            + " 条，没有一条落在你指定的站点上。「站点限定」是**拿回来之后本地筛**的"
            "（免费那条连 `site:` 语法都不认），所以命中与否取决于前几条里恰好有没有那个站。"
            "**去掉 `includeDomains` 再搜一次**，或者换更特别的词。"
            "学术文献别在这儿硬搜 —— 用 `search_papers`。",
        }
    if not items:
        return {
            "query": query,
            "results": [],
            "note": "一条都没搜到。换个说法、或者拆成更具体的关键词再试一次；"
            "**学术文献（哪篇论文 / 谁做的 / 哪一年的）换 `search_papers`** —— "
            "通用网页索引搜学术关键词会被**缩写撞名**（实测 `WCET` 搜回来的是同名协会、"
            "造口治疗师协会与词典释义）。"
            "**别把「我没搜到」当成「这件事不存在」** —— 说清是没搜到，"
            "然后按你自己已有的理解回答，并说明这部分没有出处。",
        }
    return {
        "query": query,
        "results": items,
        "note": "摘要只是**索引**，常常正好缺了你要的那一句：要引用事实、要给数字、"
        "要说「根据…」，先调 `read_web_page` 把那一页读了再开口。引用时把网址写上。",
    }


def search_papers(db, args, ctx=None) -> dict:  # noqa: ANN001
    """搜学术文献。走的是**学术索引**（OpenAlex，免密钥），不是通用网页索引。"""
    from . import websearch

    query = str(args.get("query") or "").strip()
    if not query:
        return {"error": "要搜什么？`query` 是空的。"}
    try:
        conf = websearch.resolve(db)
        items = websearch.search_papers(conf, query, count=args.get("count"))
    except websearch.SearchError as exc:
        return {"error": str(exc)}
    if not items:
        return {
            "query": query,
            "results": [],
            "note": "这个学术索引里一条都没找到。换**更通用的主题词**再试 —— "
            "它按相关度排，不要求你猜中作者写的原词（「缓存分析怎么用抽象解释做」"
            "比「the WCET paper」更容易命中）；**别只搜一次就下结论**，"
            "同一个东西换个说法常常就有了。",
        }
    return {
        "query": query,
        "results": items,
        "note": "每条给了**年份 / 作者 / 发表处 / 被引数**，摘要通常是有的"
        "（没有的就是那篇本身没提供，不是这里漏了）。**摘要不是全文**："
        "要引用具体结论、数字、方法细节，先 `read_web_page` 读那一条的链接"
        "（有开放获取 PDF 时优先给的就是它）。引用时把标题、作者、年份写上，"
        "让他能自己核对 —— 只拿摘要下结论，等于换了个地方编。",
    }


def read_web_page(db, args, ctx=None) -> dict:  # noqa: ANN001
    """把一个网页读成正文（`web_search` 之后的下一步）。"""
    from . import websearch

    url = str(args.get("url") or "").strip()
    if not url:
        return {"error": "要读哪个网址？`url` 是空的。"}
    try:
        return websearch.read_page(url)
    except websearch.SearchError as exc:
        return {"error": str(exc)}


#: 挂载分组 —— 顶栏那几个枢纽图标就是它。
#:
#: **唯一出处**：界面上的标签与顺序、`specs()` 的过滤、`call()` 的纵深防御都读这里。
#: （与资料模块的 `KINDS` 同一条规矩：抄成两处，早晚有一天对不上。）
#: `hint` 是给鼠标悬停用的一句话，说清"亮起来之后 AI 能做什么"。
GROUPS: tuple[tuple[str, str, str], ...] = (
    ("notes", "笔记", "翻笔记树、读正文、看大纲与画布、按内容找"),
    ("library", "资料", "在资料库里找条目、读它的正文与元数据"),
    ("graph", "图谱", "查概念与知识点、看前置链"),
    ("quiz", "出题", "查题库、判题、标记掌握、安排复习"),
    ("sandbox", "沙箱", "跑一段代码、渲染演示"),
    # 排在最后：前面四组都是"他的东西"，这一组是**往外看**（需要单独配密钥，
    # 见 `app/websearch.py`）。放在末尾也让顶栏那排图标的既有位置不变。
    ("web", "联网", "上外网搜、把网页读成正文"),
    # 「对话树」这一组（`docs/对话树.md` §三：**森林靠工具访问，不靠预先建边**）：
    # 跨树连接不做"预先建链接"，而是"当场查"—— 与这个项目一贯的做法一致
    #（关系靠解析、靠查，不靠标注）。它白拿两样现成的：挂载开关与权限档。
    # **排在末尾**：前面几组的既有位置一个都不动（`web` 当初也是这么来的）。
    ("trees", "对话树", "翻别的对话：找哪一段、看它的形状、取原文"),
)

GROUP_LABELS: dict[str, str] = {key: label for key, label, _ in GROUPS}
GROUP_HINTS: dict[str, str] = {key: hint for key, _, hint in GROUPS}
ALL_GROUPS: tuple[str, ...] = tuple(key for key, _, _ in GROUPS)


#: 权限档 —— 把一个工具"能动什么"分成四档，**模式就是"组 × 档"的组合**。
#:
#: * `read`    只看：查了不改变任何东西（找笔记、读材料、看掌握度）
#: * `propose` 只提案：界面上出一张凭条，**他点了才生效**（收藏、标掌握）
#: * `write`   直接写：调用即改数据（追加笔记、写答题记录）
#: * `exec`    执行：跑代码、渲染能动的演示
#:
#: 为什么非要有这一层：光靠"组"筛不出"只读模式"—— 组内读写是混装的
#: （`quiz` 组里 `get_mastery` 是只读、`grade_problem` 直接写库、`mark_mastered` 只提案）。
#: 用户的原话是"access 必须加，实际上是做一个软件内部的简单 MCP"。
ACCESS_LEVELS: tuple[str, ...] = ("read", "propose", "write", "exec")

#: **元能力**：不属于任何一块工具，但"这一版挂了任何工具"时就该在（见 `specs`）。
#: 现在只有 `run_subagent` —— 它自己不读材料、不写记录，只是把一条长工具链
#: 派出去（见 `app/subagent.py`）。极简模式（空集）里它不出现：那会儿连查都查不了。
META_TOOLS: tuple[str, ...] = ("run_subagent",)


def run_subagent(db, args, ctx=None) -> dict:  # noqa: ANN001
    """把一条长工具链派给子代理（实现见 `app/subagent.py`）。

    **权限照抄当前这一份**（`ctx` 里的 `mounts` / `allow`，由 `call` 顺手放进来的）——
    子代理不比主对话多一分权：查询模式派出去的也只能读。

    为什么要有它：主对话的上下文要维持"他是谁、聊到哪了"，而"把这件事查清楚"
    往往要十几轮工具调用 —— 那些中间过程对主线毫无用处，只会把要紧的东西挤出去。
    派活之后主线只看到这一次调用，中间过程一条都不进。
    """
    from . import subagent  # 延迟导入：它要回调这边的工具模块，放顶层会成环

    return subagent.run(
        db,
        task=str(args.get("task") or ""),
        wants=str(args.get("wants") or ""),
        mounts=(ctx or {}).get("mounts"),
        allow=(ctx or {}).get("allow"),
        tool_context=ctx,
        tools=sys.modules[__name__],
    )[1]

#: 「对话轨迹」曾经是一个工具（`read_learning_tree`），现在**不再是**：
#: 它是每轮自动挂进系统提示的仪表盘，见 `app/recursion.py` 的 `dashboard()`。
#: 理由（用户）："我认为它不应该被做成工具，对话轨迹是 llm 长期可见的用户信息仪表盘。"
#: 工具是**要模型想起来的**东西，而"这个人在怎么学"是它说每句话时都该带着的背景 ——
#: 做成工具时，模型只会在被明确要求复盘时看一眼，平时对用户一无所知。
#: 骨架渲染（`recursion.outline`）与按 id 读全文（`recursion.read_nodes`）都留着：
#: 前者给仪表盘用，后者留给"要把某一支带进这轮上下文"那个旋钮（docs/对话树.md §七）。


# ------------------------------------------------------------------ 对话树（森林那一层）
#
# `docs/对话树.md` §三：**森林靠工具访问，不靠预先建边**。跨树连接不做"预先建链接"，
# 而是"当场查" —— 于是"我上次是不是钻过这个"这件事落在四个只读工具上：
#
#     list_conversations    别处有哪些对话（先看有什么）
#     search_conversations  按内容找（"我到底在哪儿学过这个"）
#     read_tree             看某棵树的形状（主线怎么走、在哪儿下钻、每支多深）
#     read_tree_nodes       按 id 取某几条的原文（骨架只给预览）
#
# 与仪表盘（`recursion.dashboard`）的分工必须说清：仪表盘是**这一条**对话的轨迹，
# 每轮自动带上、不用它去查；这一组是**别的**对话 —— 它得自己想到去翻，所以
# `routers/chat.py` 的 `GROUP_PROMPTS["trees"]` 里写明了"什么时候该翻、怎么翻才对"。


def _tree_of(db, ctx, args):  # noqa: ANN001, ANN202
    """`conversationId` → Conversation；缺省 = **当前这一条**。返回 `(会话, 为什么没有)`。

    不给"猜一条"的余地：认不出来就把话说清楚 —— 模型据此能自己改正（换个 id、或者先
    `list_conversations` 看一遍），这比抛异常、或者默默读一条别的对话好得多。
    """
    raw = str((args or {}).get("conversationId") or "").strip()
    if not raw:
        raw = str((ctx or {}).get("conversationId") or "").strip()
    if not raw:
        return None, "没给 conversationId，这一轮也不知道当前是哪条对话。"
    try:
        cid = uuid.UUID(raw)
    except (TypeError, ValueError):
        return None, "conversationId 不是合法的 id：" + raw
    conv = db.get(Conversation, cid)
    if conv is None:
        return None, "没有这条对话。先用 list_conversations 看有哪些，再用它给的 id。"
    return conv, ""


def list_conversations(db, args, ctx=None) -> dict:  # noqa: ANN001
    """他和我坐下过的每一段（最近动过的在前）。

    为什么要给**条数**：那是"这一段值不值得读"最省的一个判断 —— 一条对话三两轮，
    看形状就够了；几十条的那种，先看它长什么样再决定读哪一段。
    """
    limit = _clamp((args or {}).get("limit"), 1, 50, 20)
    rows = db.execute(
        select(Conversation, func.count(Message.id))
        .outerjoin(Message, Message.conversation_id == Conversation.id)
        .group_by(Conversation.id)
        .order_by(Conversation.updated_at.desc())
        .limit(limit)
    ).all()
    now = str((ctx or {}).get("conversationId") or "")
    return {
        "items": [
            {
                "conversationId": str(conv.id),
                "title": conv.title or "未命名",
                "messages": count,
                "updatedAt": recursion.stamp(conv.updated_at),
                "current": str(conv.id) == now,
            }
            for conv, count in rows
        ],
        "note": "`current: true` 的那条就是你们现在正说着的。要看某一棵的形状用 read_tree，"
                "要取某几条的原文用 read_tree_nodes。",
    }


def search_conversations(db, args, ctx=None) -> dict:  # noqa: ANN001
    """在**所有对话**的消息正文里搜一段字 —— 「我上次是不是讲过这个」。

    复用 `/api/chat/search` 那一份实现（它就在 `routers/chat.py`，按内容找东西的规矩
    只有那一处）。延迟导入是因为 `routers` 那一层要 import 本模块，顶层 import 会成环。
    """
    from .routers import chat as chat_router

    query = str((args or {}).get("query") or "").strip()
    limit = _clamp((args or {}).get("limit"), 1, 50, 12)
    found = chat_router.search_messages(db=db, q=query, limit=limit)
    now = str((ctx or {}).get("conversationId") or "")
    return {
        "query": found.get("query") or query,
        "items": [
            {
                "conversationId": one.get("conversationId"),
                "messageId": one.get("messageId"),
                "title": one.get("title"),
                "role": one.get("role"),
                "snippet": one.get("snippet"),
                "at": recursion.stamp(
                    datetime.fromtimestamp(int(one.get("atMs") or 0) / 1000, tz=UTC)
                ),
                "current": str(one.get("conversationId")) == now,
            }
            for one in (found.get("items") or [])
        ],
        "note": found.get("note")
        or "命中点只看得到一句上下文；要整段就用 read_tree_nodes 按 messageId 取原文。",
    }


def read_tree(db, args, ctx=None) -> dict:  # noqa: ANN001
    """读**一棵对话树**的形状：主线怎么走的、在哪儿下钻过、每一支各自多深。

    骨架里每条都是预览（几十字）。要逐字看的，把它行首那个 `#id` 交给
    `read_tree_nodes` —— 两段式（先看形状，再按 id 取）就这两步。
    """
    conv, why = _tree_of(db, ctx, args)
    if conv is None:
        return {"error": why}
    return {
        "conversationId": str(conv.id),
        "title": conv.title or "未命名",
        "tree": recursion.outline(db, conv),
        "note": "这是《" + (conv.title or "未命名") + "》这棵树的骨架。",
    }


def read_tree_nodes(db, args, ctx=None) -> dict:  # noqa: ANN001
    """按 id 取某几条消息的**原文**（骨架只给预览，这一步才读全文）。

    一次别点太多：这些原文会跟着历史被每一轮重放（见 `app/agent_loop.py` 的预算），
    通常取"分叉点的上一轮"那几条就够。
    """
    conv, why = _tree_of(db, ctx, args)
    if conv is None:
        return {"error": why}
    ids = (args or {}).get("ids") or []
    if isinstance(ids, (str, int)):
        ids = [ids]
    return {
        "conversationId": str(conv.id),
        "tree": recursion.read_nodes(db, conv, list(ids)),
    }


REGISTRY = {
    "list_notes": {
        "access": "read",
        "group": "notes",
        "fn": list_notes,
        "description": "有哪些笔记库、某个库里有哪些篇。"
        "用户说\"我笔记里记过什么\"、或者你不知道该去哪个库找时用它 —— "
        "`search_notes` 是按关键词搜，这个给的是全貌。",
        "parameters": {
            "type": "object",
            "properties": {
                "lib": {"type": "string", "description": "哪个笔记库（可省；省了只列库与各自篇数）"},
                "limit": {"type": "integer", "description": "最多几篇（默认 200）"},
            },
        },
    },
    "read_note": {
        "access": "read",
        "group": "notes",
        "fn": read_note,
        "description": "读一篇笔记的正文（Markdown），一并给大纲与反链。"
        "`search_notes` 只告诉你命中在哪，**要看他写了什么必须调这个** —— "
        "别让用户自己去打开。"
        "长笔记一次给不完：结果里的 `note` 会写清这次给到第几字，"
        "**要接着往下读就再调一次、把 startChar 传成它给的那个数**"
        "（别只看开头就开始总结）。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "笔记在库里的相对路径，如 信号与系统/卷积.md",
                },
                "lib": {"type": "string", "description": "哪个笔记库（可省）"},
                "maxChars": {
                    "type": "integer",
                    "description": "这一次最多给多少字（默认 40000，上限 100000）",
                },
                "startChar": {
                    "type": "integer",
                    "description": "从第几个字开始（默认 0）。要接着上一次往下读，就传上一次 note 里给的那个数",
                },
            },
            "required": ["path"],
        },
    },
    "search_notes": {
        "access": "read",
        "group": "notes",
        "fn": search_notes,
        "description": "在**用户自己的笔记**里按内容找（跨所有笔记库）。"
        "用户说\"我笔记里写过\"\"我之前记过\"\"找一下我的笔记\"时用它；"
        "要找的是资料根下的材料（PDF、手册），用 search_material。"
        "返回库名、路径、标题与片段。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "关键词或短语"},
                "lib": {"type": "string", "description": "限定某个笔记库（可省，省了就全找）"},
                "limit": {"type": "integer", "description": "最多几条（默认 12）"},
            },
            "required": ["query"],
        },
    },
    "write_note": {
        "access": "write",
        "group": "notes",
        "fn": write_note,
        "description": "把一段话**追加到某篇笔记的末尾**。只在用户说\"记到我的笔记里\""
        "\"把这段结论挂上去\"时用它 —— **要改已有的内容用 `edit_note`**"
        "（这个只会往末尾堆，改不了中间）。写入前会自动留快照，可以撤回。"
        "**先跟用户确认写哪一篇**（路径要准确），别自己挑一篇就写。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "笔记在库里的相对路径，如 信号与系统/卷积.md"},
                "text": {"type": "string", "description": "要写进去的内容（Markdown）"},
                "lib": {"type": "string", "description": "哪个笔记库（可省）"},
            },
            "required": ["path", "text"],
        },
    },
    "edit_note": {
        "access": "write",
        "group": "notes",
        "fn": edit_note,
        "description": "改一篇笔记里的**某一处**：给一段原文（old），换成新的（new）。"
        "用户说\"把那段改一下\"\"这句不太对\"\"删掉这句\"\"把 X 换成 Y\"时用它；"
        "只想追加到末尾才用 `write_note`。"
        "`old` 必须是**从 `read_note` 结果里逐字复制**的一段原文 ——"
        "不是行号、也不是\"第三小节\"这种描述。找不到、或同一段出现多次，"
        "它会**拒绝并把原因说清**（列出那几处长什么样），照它说的多带点上下文再试，"
        "别换个说法硬撞。`new` 给空串就是删掉这一段，所以它**不能省**。"
        "改前自动留快照；YAML 头（标题/标签）不归它管。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "笔记在库里的相对路径，如 信号与系统/卷积.md"},
                "old": {
                    "type": "string",
                    "description": "要被替换掉的那段原文，逐字复制；要够独特（同一段出现多次会被拒绝）",
                },
                "new": {
                    "type": "string",
                    "description": '换成什么（Markdown）。给空串 "" = 删掉这一段',
                },
                "lib": {"type": "string", "description": "哪个笔记库（可省）"},
                "all": {
                    "type": "boolean",
                    "description": "这段原文出现多次时，明确表示全都要改（默认拒绝，好让你先看清是哪几处）",
                },
            },
            "required": ["path", "old", "new"],
        },
    },
    "attach_material": {
        "access": "read",
        "group": "library",
        "fn": attach_material,
        "description": "把资料库里的一份资料读进来：给它的元数据与一段正文，"
        "这样后面的回答能**基于原文**而不是凭印象。用户说\"看那份规范\""
        "\"把这篇论文挂上\"时用它；不知道引用键就先 search_material。",
        "parameters": {
            "type": "object",
            "properties": {
                "citekey": {"type": "string", "description": "引用键（首选）"},
                "name": {"type": "string", "description": "或者文件名/标题的一部分"},
                "chars": {"type": "integer", "description": "取多少字正文（默认 6000）"},
            },
        },
    },
    "search_knowledge": {
        "access": "read",
        "group": "graph",
        "fn": search_knowledge,
        "description": "在知识空间里按关键词找概念与知识点。用户问到一个术语、"
        "一个机制名、或你不确定它在这套材料里怎么表述时，先用它。"
        "返回跨材料的概念（concepts）与它在各份材料里的出现（points）。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "关键词，如 circular buffer、noc"},
                "limit": {"type": "integer", "description": "最多几条，默认 8"},
            },
            "required": ["query"],
        },
    },
    "get_point_detail": {
        "access": "read",
        "group": "graph",
        "fn": get_point_detail,
        "description": "取一个知识点或概念的详情：它是什么、在哪份材料的哪几行出现、"
        "挂了几道题、与哪些概念有前置/包含/易混关系。要讲清一处机制时用它拿出处。",
        "parameters": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "点或概念的 key（来自 search_knowledge）"}
            },
            "required": ["key"],
        },
    },
    "get_existing_questions": {
        "access": "read",
        "group": "quiz",
        "fn": get_existing_questions,
        "description": "题库里已有的题（默认不含答案，避免推题时剧透）。"
        "用户想练某个知识点、或你想看看已有题长什么样时用它。",
        "parameters": {
            "type": "object",
            "properties": {
                "pointKey": {"type": "string", "description": "限定某个知识点的 key，可省略"},
                "layer": {"type": "string", "description": "识记 / 理解 / 应用 / 迁移，可省略"},
                "type": {
                    "type": "string",
                    "description": "题型：single / multi / blank / short / problem，可省略。"
                    "想看大题就传 problem —— 不传时按 id 排，抽样里可能一道大题都没有。",
                },
                "limit": {"type": "integer", "description": "最多几道，默认 5"},
                "includeAnswer": {"type": "boolean", "description": "讲解时设为 true（同时给完整题干）"},
            },
        },
    },
    "get_mastery": {
        "access": "read",
        "group": "quiz",
        "fn": get_mastery,
        "description": "用户在各知识点上的掌握度与档位（new/learning/familiar/mastered）。"
        "想判断该给他讲多深、该先补哪儿时用它。",
        "parameters": {
            "type": "object",
            "properties": {
                "pointKeys": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "限定几个知识点，为空则返回最弱的若干",
                }
            },
        },
    },
    "get_due_reviews": {
        "access": "read",
        "group": "quiz",
        "fn": get_due_reviews,
        "description": "按间隔重复算法到期该复习的题。用户问「今天该复习什么」时用它。",
        "parameters": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "最多几条，默认 10"}},
        },
    },
    "run_python": {
        "access": "exec",
        "group": "sandbox",
        "fn": run_python,
        # 包名单**按常量生成，一个字都别写死**。从前的原文是"固定一个子集：numpy 与
        # scipy；别的装不了 —— 没有 pandas、没有 matplotlib（要图就用 render_demo 写 JS）"，
        # 而 matplotlib 早就进了预载。后果实测过一次：用户说"用一下 matplotlib"，
        # 模型照着自己那段过期的话回答"沙箱里没有 matplotlib，装不了别的东西"，
        # 明明能画也不画了。**名单会变，散文不会自己跟着变** —— 所以这里 join 常量。
        "description": "在沙箱里**真跑**一段 Python（Pyodide：浏览器里跑 CPython）。"
        "用户说「跑一段脚本」「算一下」「验证一下这个算法」「试试这段代码」时**直接用它**，"
        "不要推辞、也不要让他自己去写页面。只给核心逻辑，用 print 出结果 —— "
        "样板（加载运行时、接 stdout、显示报错）由工具负责。"
        "**本机就有的包（开箱即用，不必点名）**：" + "、".join(PRELOAD_PACKAGES) + "；"
        "别的装不了（要编译、要系统库的没有；也没有文件系统与网络）。"
        "面板开头会报出实际版本。\n"
        "**要画图就直接 `import matplotlib.pyplot as plt` 再 `plt.plot(...)`** —— "
        "壳会把图收走贴在面板上，**不用** `savefig`、不用 base64（最多 3 张）；"
        "图里**可以写中文**（壳里装好了一份简体宋体，标题、图例、轴标签、刻度都排得出来；"
        "用常见简体字，生僻字与日文假名可能没字形）。"
        "**字体那几行你不用写** —— `font.sans-serif`、`axes.unicode_minus`、`mathtext.*`"
        "（对数轴的 `10^-1` 归 mathtext 管，是另一套字体栈）壳都设好了，"
        "而且**每次运行前都会复核一遍**；你在脚本里再写一遍没用也多余，画就是了。"
        "**电路图用 `schemdraw`**（网表式地拼元件与连接，别拿 plt 手画电阻）；"
        "**推导用 `sympy`**。\n"
        "面板底部会把时间分成三段报出来（运行时 / 依赖 / 代码）。"
        "**scipy 的子模块（signal、optimize 这些）第一次 import 要 1–2 秒** —— "
        "那是它的固有成本，不是沙箱慢；跟他说清楚，别让他以为是环境有问题。\n"
        "不要说「跑出来了，结果是 X」这种编的话 —— 实测发生过：声称 numpy 可用，"
        "面板上却是 No module named 'numpy'。你只需说清写这段在验证什么。"
        "**输出会自动回填到那条消息上**（界面上直接显示），并且**在你下一轮说话时"
        "进你的上下文** —— 所以不要让他复制粘贴、也不要问他「结果是什么」，"
        "下一条消息你自己就能看到。",
        "parameters": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "要跑的 Python 源码（用 print 输出）"},
                "title": {"type": "string", "description": "一句话说明这段在算什么"},
                "packages": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "一般不用填 —— 上面那份名单都开箱即用。"
                    "只有确实需要名单里的包、又是动态导入（`__import__`）时才列，"
                    "且必须在名单内 —— 名单外的会被剔除并告知。",
                },
            },
            "required": ["code"],
        },
    },
    "render_demo": {
        "access": "exec",
        "group": "sandbox",
        "fn": render_demo,
        "description": "产出一个**能动的演示**（跑在沙箱 iframe 里）。"
        "机制里有空间/时间结构时用它：数据怎么流、怎么切、怎么重叠、流水线怎么排、瓶颈在哪 —— "
        "一张能动的图胜过三段文字。"
        # 分工写清楚：用户说"用一下 matplotlib"时，模型从前会把它推到这里来（因为那份
        # 说明里写着"没有 matplotlib"）。静态的图归 run_python，这里留给要动的。
        "**静态的图不要用这里**：函数曲线、直方图、多组数据对比、频谱 —— 用 `run_python` "
        "里的 matplotlib（`plt.plot(...)` 一行就出图，还能直接写中文标题与轴标签）。"
        "**TikZ 与 pgfplots 更不要放这里** —— 演示里的公式是 **KaTeX** 排的，它**不认 TikZ**："
        "`\\begin{tikzpicture}` / `axis` / `tikzcd` / `circuitikz` 放进去只会显示成源码或报错"
        "（这个工具会**当场拒掉**）。那类图一律写在**正文的 `$$`** 里，它们由真 TeX 引擎编。"
        "演示里真要画图，用 d3 / SVG / Canvas 自己画。"
        "**不要**用它讲定义、结论或代码逐行解释；要跑 Python 用 run_python。**除非**用户就是"
        "想看它动起来（数据怎么流、状态怎么变）。\n"
        "**沙箱里已经备好一套前端套件，由服务端自动注入 —— 你不要写任何 `<script src>`，"
        "也不要引 CDN。** 可用的是：React 18 + JSX（写在 `<script type=\"text/babel\">` 里）、"
        "htm、d3 v7、Tailwind，以及 `window.QFKit`：\n"
        "* `QFKit.mount(<App/>)` —— 一行挂载\n"
        "* `QFKit.Frame / Card / Row / Btn / Legend / KV / Note / Chip` —— 布局与信息块\n"
        "* `QFKit.useTicker(speed, {paused})` —— 帧驱动步进（**不要**用 setInterval 改 DOM）。**默认每秒 6 步（约 167ms 一步），照用就行** —— 别为了「显得流畅」把它调快：一步快过 100ms 就只剩糊影了（从前基准是每秒 60 步、骨架里写着 1，于是所有演示都闪得看不清，这是被抱怨过的）\n"
        "* `QFKit.scales({width,height,xDomain,yDomain})` + `QFKit.useAxis(ref,{scales})` —— "
        "坐标轴与数据**共用同一组比例尺**（手画坐标轴十次九次是歪的）\n"
        "* `QFKit.colors` —— 色板（`series` 给多条序列）\n"
        "* `QFKit.Plot` —— **坐标图就用它**："
        "`<QFKit.Plot width={640} height={300} xDomain={[0,50]} yDomain={[0,1]} xLabel=\"时钟\">"
        "{({scales}) => <g>{/* 用 scales.x()/scales.y() 算坐标 */}</g>}</QFKit.Plot>`。"
        "边距与坐标轴的位置它都算好了 —— 自己拿 `scales` 拼、又漏了那次 translate，"
        "就会把 y 轴压在画布左边缘（刻度被切掉半个，实测发生过）。\n"
        "* `QFKit.Math` / `QFKit.tex` —— **公式**（KaTeX 已备好，与正文同一份）："
        "`<QFKit.Math tex=\"x^2+y^2\" />`（`inline` 传 true 随文字走）；"
        "要在别处用就 `QFKit.tex('…')` 拿 HTML，塞进 `dangerouslySetInnerHTML`。\n"
        "**动画节奏 —— 每拍多少毫秒由你判断**：一拍一拍的东西用 "
        "`QFKit.useStepper(每拍毫秒)`（离散、整数步；连续插值才用 useTicker）。"
        "先数清这个演示一共演几拍，再让整段落在 **3–10 秒**：20 个数冒泡 ≈ 200 拍，"
        "那就一次跳好几拍、或把规模讲小，而不是把每拍压到 20ms —— 看不清就白画了。"
        "拿不准给 400ms。另配一个播放/暂停或单步控件，能停就让它停。\n"
        "**本站的视觉风格 —— 这是设计规范，别自己发明**。一句话："
        "**克制的双主题（跟用户设置走）+ 面板与细边框 + 留白 + 品牌色只做点缀**。"
        "演示要像「这个应用里的一张卡片」，不是「一个网站」。\n"
        "* **主题**：页面已经按用户当前主题配好（白底或深底）。**不要自己设 `background` / `color`**，"
        "用 `QFKit` 的组件与 `QFKit.colors` —— 它是**活的**，白底主题下取到的就是白底那套。"
        "白底上要把青色当**文字/线条**用时用 `colors.pri3`（更深的同色）：`colors.pri` 是亮青，"
        "衬白底对比度不够，只适合做填充块。要更多条序列就靠**亮度**拉开，不要新造色号。\n"
        "* **颜色是硬规矩：每一个色都得来自 `QFKit.colors` 或 `currentColor`，不许写 `#` 开头的裸色号**。"
        "实测有一份演示写了 65 个裸色号、`QFKit.colors` 一次没用 —— 那些色是照深底挑的，"
        "用户切到白底主题后就成了白字白底（原话：「底是白的，但是 llm 画出来的东西里面，字也变白了」）。"
        "色块上的字用 `colors.bg`（与那块底色相反）或 `currentColor`，**不要写 `#fff`**。"
        "套件渲染完会**自动**把读不清的文字换成当前主题的字色 —— 那是兜底，不是许可。\n"
        "* **层次靠背景，不靠阴影**：三档背景（`bg` / `panel` / `panel2`）已经够分出层级；"
        "`QFKit` 的卡片自带那一档**唯一允许**的阴影，**别再叠 `box-shadow`** —— "
        "满屏浮起来的面板是最典型的「AI 味」。\n"
        "* **边框要看得见**：分隔与轮廓用 `colors.line` / `colors.line2`，别拿阴影代替边框。\n"
        "* **圆角只有三档**：8 / 12 / 18（小控件 / 卡片 / 大面板），同一层里必须一致。\n"
        "* **间距只用 4 的倍数**：8 / 12 / 16 / 24，同级元素之间用同一个值。"
        "不要 7、13、22 这种数 —— 对不齐就是这么来的。\n"
        "* **字号只有三档**：标题 18、正文 14、注释 12，别为了「层次感」再加第四档。\n"
        "* **动效**用套件里那一条（180ms 的同一条缓动），别自己写 `transition: all .3s` 那种。\n"
        "* **留白是一部分**：一屏只讲一件事，宁可少画点。\n"
        "* **对齐**：正文左对齐、数字右对齐；居中只用在标题那一层。\n"
        "* **不要**：渐变背景、霓虹发光、玻璃拟态（毛玻璃）、emoji 当图标、"
        "硬编码 `#fff` / `#000`、卡片里再套卡片。\n"
        "**照这个骨架写，别从空白页开始**：\n"
        "<div id=\"app\"></div>\n"
        "<script type=\"text/babel\">\n"
        "const {useState} = React;\n"
        "function App() {\n"
        "  const [run, setRun] = useState(true);\n"
        "  const t = QFKit.useTicker(1, {paused: !run});\n"
        "  return (<QFKit.Frame title=\"…\" caption=\"你在看什么：…\"\n"
        "      right={<QFKit.Btn on={run} onClick={() => setRun(!run)}>{run?'暂停':'播放'}</QFKit.Btn>}>\n"
        "    <QFKit.Card title=\"机制\"><svg className=\"qf-svg\" viewBox=\"0 0 640 300\">{/* 坐标用 d3 算 */}</svg></QFKit.Card>\n"
        "  </QFKit.Frame>);\n"
        "}\n"
        "QFKit.mount(<App/>);\n"
        "</script>\n"
        "（JSX 里插值必须写成 `{}`，例如 `{\'共 \' + n + \' 拍\'}` —— "
        "直接写 `+` 会原样显示在页面上，实测踩过。）"
        "三条硬规矩：**手不要画坐标轴/刻度/图例**（用 QFKit 的）；"
        "**动画用 useTicker + React 状态**，不要 setInterval 改 DOM；"
        "**至少给一个控件**（播放/切换/拖拽）并写一句「你在看什么」。"
        "尺寸自适应（viewBox + 百分比）。上限 " + str(DEMO_MAX_CHARS) + " 字。",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "演示的标题（一句话）"},
                "html": {
                    "type": "string",
                    "description": "页面正文（套件自动注入，别写 <script src>）；"
                    "推荐 `<div id=\"app\"></div>` + 一段 `text/babel` 脚本",
                },
            },
            "required": ["html"],
        },
    },
    "explore_graph": {
        "access": "read",
        "group": "graph",
        "fn": explore_graph,
        "description": "从一个概念/知识点出发，走**语义关系**看邻域：前置、后继、组成部分、易混、实现。"
        "「我该先学什么」「这两块什么关系」「为什么我学不懂 X」用它；按词找节点用 search_knowledge。"
        "每个邻居都带 relation 与 band（你的掌握档位，取该概念下最弱的点），"
        "所以「哪个前置还没打牢」可以直接看出来。"
        "默认只看 requires / part_of / implements；contrast_with 与 co_occurs "
        "大多是机械派生的（前者大量来自「题目选项里一起出现过」），要看需显式列进 kinds。",
        "parameters": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "概念或知识点的 key"},
                "query": {
                    "type": "string",
                    "description": "不记得 key 时给名字片段（key 与 query 给一个即可）",
                },
                "depth": {"type": "integer", "description": "走几层，默认 1、最多 2"},
                "kinds": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "边类型子集，默认 requires/part_of/contrast_with/implements",
                },
                "limit": {"type": "integer", "description": "最多几个邻居，默认 12"},
            },
        },
    },
    "search_material": {
        "access": "read",
        "group": "library",
        "fn": search_material,
        "description": "在**材料原文**里检索（`depth=出题` 的那一档：我在学的、值得精读与出题的材料），"
        "返回命中的行区间与原文片段。两路合并："
        "**字面**（你给的原词在原文里出现过）与**向量**（你换个说法问同一件事 —— "
        "「上三角屏蔽」与 causal mask 也算命中，不要求词一样）。"
        "被两路都找到的排在只被一路找到的前面，每条结果的 `via` 写着它是被哪条找到的。"
        "**另一个书架（论文 / 白皮书 / 别人的资料）用 `search_library`** —— 两边检索方式完全一样，"
        "**在一个里没找到就去另一个里再问一次**，别只搜一个就说「没有」。"
        "想知道概念/点在图谱里的位置，用 `search_knowledge`。"
        "词要给得特别（exp_approx_mode、circular buffer 这种原词最灵）—— "
        "字面那一路要求所有词在**同一行**同时出现才算命中，放宽就少给一个词。"
        "返回里的 scanned/total 说明这次扫了多少份：没扫完就别说「材料里没有」。"
        "**semantic 为 false 说明向量那一路这次没参与**（本地小模型还没就绪）——"
        "那种情况下「换个说法就搜不到」是正常的，**别当成材料里没有这件事**。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "问一句话或给关键词都行：原词走字面，换个说法走向量"},
                "slug": {"type": "string", "description": "只在这份材料里找（可省略）"},
                "limit": {"type": "integer", "description": "最多几段，默认 5"},
            },
            "required": ["query"],
        },
    },
    "search_library": {
        "access": "read",
        "group": "library",
        "fn": search_library,
        "description": "在**资料库**里检索，返回命中的行区间与原文片段。"
        "和 search_material 的**唯一区别是看哪个书架**："
        "这个查 `depth=检索` 的那一档（论文 / 白皮书 / 别人的资料 —— 拿来读、拿来引用，"
        "但不必出成题），那个查 `depth=出题` 的那一档（我在学的、值得精读与出题的材料）。"
        "**两边的检索方式完全一样**（字面 + 向量融合，`via` 会说清是哪条找到的）。"
        "**在一个里没找到，就去另一个里再问一次** —— 它们是两个书架，不是同一堆东西的两种搜法；"
        "只搜一个就说「没有」，是这里最容易犯的错。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "问一句话或给关键词都行：原词走字面，换个说法走向量"},
                "slug": {"type": "string", "description": "只在这一条资料里找（引用键，可省略）"},
                "limit": {"type": "integer", "description": "最多几段，默认 5"},
            },
            "required": ["query"],
        },
    },
    "read_material": {
        "access": "read",
        "group": "library",
        "fn": read_material,
        "description": "读材料原文的某几行（默认从 startLine 起 60 行）。"
        "要用原文支撑结论时：先 search_material 拿到行号，再用它把上下文读全；"
        "也可以顺着刚读到的区间继续往下读。",
        "parameters": {
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "材料 slug"},
                "startLine": {"type": "integer", "description": "起始行（从 1 起）"},
                "endLine": {
                    "type": "integer",
                    "description": "结束行（含；可省略，默认往下读 60 行）",
                },
            },
            "required": ["slug"],
        },
    },
    "grade_problem": {
        "access": "write",
        "group": "quiz",
        "fn": grade_problem,
        "description": "把一次**大题批改**的逐问判定写进答题记录（掌握度与间隔重复因此"
        "与其它题走同一条路）。批完**必须**调它，否则这次作答不算数 —— 记录是事实，"
        "讲评只是投影。verdict 取 correct / partial / wrong，score 是该问的得分率 0–1；"
        "comment 写一句依据（引材料要看得到出处）。",
        "parameters": {
            "type": "object",
            "properties": {
                "questionId": {"type": "string", "description": "题号，例如 tt-arch-0082"},
                "verdicts": {
                    "type": "array",
                    "description": "逐问判定",
                    "items": {
                        "type": "object",
                        "properties": {
                            "index": {"type": "integer", "description": "第几问（从 1 起）"},
                            "verdict": {"type": "string", "enum": ["correct", "partial", "wrong"]},
                            "score": {"type": "number", "description": "该问得分率 0–1"},
                            "comment": {"type": "string", "description": "一句依据（缺什么/对在哪）"},
                        },
                        "required": ["index", "verdict"],
                    },
                },
            },
            "required": ["questionId", "verdicts"],
        },
    },
    "flag_question": {
        "access": "propose",
        "group": "quiz",
        "fn": flag_question,
        "description": "提议把某道题加入/移出**收藏夹**（=「这题值得再看」的标记，刷题页里叫收藏夹）。"
        "注意这是**提议**：界面上会出现一张待确认的凭条，他点了才真的生效。"
        "所以要说「要不要把这题收起来」，不要说「我已经帮你收藏了」。",
        "parameters": {
            "type": "object",
            "properties": {
                "questionId": {"type": "string", "description": "题号，例如 tt-arch-0012"},
                "on": {"type": "boolean", "description": "true=加入收藏，false=移出。默认 true"},
            },
            "required": ["questionId"],
        },
    },
    "mark_mastered": {
        "access": "propose",
        "group": "quiz",
        "fn": mark_mastered,
        "description": "提议把某道题标成**已掌握**（错题本里不再催它）或取消这个标记。"
        "他说「这题我会了，别老考我」时用它。"
        "**这只是提议**，他点了才生效；而且它不改作答记录、不改掌握度 —— "
        "那两样只从作答长出来，不要把它说成「你已掌握这个知识点」。",
        "parameters": {
            "type": "object",
            "properties": {
                "questionId": {"type": "string", "description": "题号，例如 tt-arch-0012"},
                "on": {"type": "boolean", "description": "true=标为已掌握，false=取消。默认 true"},
            },
            "required": ["questionId"],
        },
    },
    "push_question": {
        "access": "read",
        "group": "quiz",
        "fn": push_question,
        "description": "推一道题给他做（返回一张**可作答的题卡**，不含答案）。"
        "他说「考考我」「来道题」「练一道」时用它；你刚讲完一段机制、想确认他确实懂了时也可以主动推。"
        "题卡由界面渲染、他答完结果会自动进答题记录 —— 所以**不要**报答案，也不要替他念选项。"
        "大题（problem）**也能推**：卡片会按「每问一个输入区」渲染，他答完可以点"
        "「发送给 AI」把作答发给你批改 —— 所以推了大题之后就等他这一步，"
        "**不要**自己先把推导念出来。"
        "**别凭印象说题库里有没有某类题**：`get_existing_questions` 会连全量统计一起返回。",
        "parameters": {
            "type": "object",
            "properties": {
                "pointKey": {
                    "type": "string",
                    "description": "限定知识点（可省略；省略时在你刚讲的范围里找）",
                },
                "layer": {"type": "string", "description": "识记 / 理解 / 应用 / 迁移"},
                "wing": {"type": "string", "description": "基础 / 应用 / 综合 / 创新"},
                "type": {
                    "type": "string",
                    "description": "题型：single / multi / blank / short / problem。"
                    "**要大题就传 problem**（题库里 34 道，全在应用层）—— "
                    "不传的话按 id 排序取，推出来的多半是选择题。",
                },
                "maxDifficulty": {"type": "integer", "description": "难度上限 1–5"},
            },
        },
    },
    "create_question": {
        "access": "read",
        "group": "quiz",
        "fn": create_question,
        "description": "**自己出一道题**给他做（挂成一张**临时题卡**：能直接作答，默认不进题单）。"
        "题库里没有贴切的题、或你想就着刚讲的内容专门考他一个点时用它 —— "
        "`push_question` 是从**已有题库**里挑，这个是**现编**。\n"
        "写得像一道真题：题干自足（不依赖你刚说的原话）、选项互斥、"
        "每个错项对应一种典型误解（在 `explanation` 里说清\"错的人是怎么想的\" —— "
        "那比\"正确答案是 B\"有用得多）。\n"
        "题型用 single / multi / blank / short / problem；要多问的大题用 problem"
        "（`questions` 里每问给 stem 与 reference）。\n"
        "答案随题目一起交给界面（它要判分），所以**不要在正文里报答案**；"
        "他说题目哪里不对，就**改一版重新调这个工具**（每次都是新草稿）。",
        "parameters": {
            "type": "object",
            "properties": {
                "pointKey": {"type": "string", "description": "这道题冲着哪个知识点（可省略）"},
                "question": {
                    "type": "object",
                    "description": "题目本身：type / stem / options / answer / explanation / hint "
                    "/ layer / wing / difficulty / questions（大题的小问）",
                },
            },
            "required": ["question"],
        },
    },
    "web_search": {
        "access": "read",
        "group": "web",
        "fn": web_search,
        "description": (
            "上外网搜一次，拿回几条结果的标题、网址与摘要。\n"
            "**什么时候用**：要的东西不在他的材料里、也不是通用常识 —— "
            "最新的版本 / 现在的推荐做法 / 某个型号的参数 / 一篇论文的出处 / "
            "一个刚出的说法。他自己的笔记、资料、图谱里查不到时，这是第二条路。\n"
            "**什么时候别用**：材料库与知识图谱里查得到的事（那几路更准、还带行号与出处）；"
            "以及**关于他自己的事**（他学到哪、记过什么，那些只有本机那几路知道，"
            "网上不会有答案）。\n"
            "**摘要不是依据**：它常常正好缺了你要的那一句。要引用事实、要给数字、"
            "要说「根据…」，先 `read_web_page` 把那一页读了再开口 —— "
            "只拿摘要下结论，等于换了个地方编。引用时**把网址写上**，让他能自己点开核对。\n"
            "**一次搜一个点**：几个不相关的问题就搜几次，别把一串关键词堆进一次搜索。"
            "搜不到就换个说法再来，或者直说没搜到。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索词。像你在搜索框里会打的那样，越具体越好"
                    "（带上年份、版本号、机构名这类能缩小范围的字）",
                },
                "count": {"type": "integer", "description": "要几条（1-20，默认 6）"},
                "includeDomains": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "只在这几个站点里找（可省），例如 [\"arxiv.org\", \"docs.python.org\"]",
                },
                "excludeDomains": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "别要这几个站点的结果（可省），例如 [\"zhihu.com\"]",
                },
            },
            "required": ["query"],
        },
    },
    "search_papers": {
        "access": "read",
        "group": "web",
        "fn": search_papers,
        "description": (
            "搜**学术文献**（论文 / 综述 / 技术报告），拿回标题、作者、年份、发表处、"
            "被引数与摘要。免密钥。\n"
            "**它与 `web_search` 不是同一个东西的两种搜法，是两个索引。**\n"
            "**什么时候必须用它**：要找的是**文献** —— 哪篇论文、谁做的、哪一年的、"
            "某个方法的出处、某个领域的综述。通用网页索引在这件事上是"
            "**结构性做不到**：它按网页排序，而学术关键词常是**缩写或人名**。"
            "实测搜 `WCET` 回来的是同名协会、世界造口治疗师协会与词典释义；"
            "搜 `Ferdinand cache behavior prediction` 回来的是斐迪南大公与一部动画电影。"
            "**而这些都是「成功的搜索」** —— 有结果、有摘要、不报错，"
            "所以从「有没有结果」根本看不出错了。\n"
            "**什么时候用 `web_search`**：现在怎么做、某个工具或型号的参数、官方文档、"
            "下载地址这类**网页**上的东西。\n"
            "**词的给法不一样**：这里按**相关度**排序，所以给**主题词**"
            "（「cache analysis abstract interpretation」）比给精确短语或作者名更容易命中；"
            "而在 `web_search` 里要挑特别的原词。\n"
            "**摘要不是全文**：要引用具体结论、数字、方法细节，先 `read_web_page` "
            "读那一条的链接（有开放获取 PDF 时优先给的就是它）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "主题词，像论文检索框里会打的那样（英文命中率更高）",
                },
                "count": {"type": "integer", "description": "要几条（1-20，默认 6）"},
            },
            "required": ["query"],
        },
    },
    "read_web_page": {
        "access": "read",
        "group": "web",
        "fn": read_web_page,
        "description": (
            "把一个网页读成正文文本 —— `web_search` 之后的下一步。\n"
            "**为什么非有它不可**：摘要常常正好缺了你要的那一句，而只拿摘要下结论就是在编。"
            "搜到像样的来源就把它读了，再开口；一次读一页，读哪几页由你挑（两三页通常够了）。\n"
            "**它给的是抽出来的正文**（启发式）：导航、脚本、样式已经去掉，但有些站点抽不干净。"
            "返回里的 `note` 说「抽出来的正文很少」时，那一页多半是脚本渲染的 —— "
            "**别当成「这一页没内容」**，换个来源或者直说读不到。\n"
            "只能读 http / https，**本机与内网的地址读不了**（这是刻意的，别去试）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "要读的网址（http / https）。用 `web_search` 回来的那个原样填。",
                },
            },
            "required": ["url"],
        },
    },
    # ---- 对话树：翻**别的**对话（这一组见 `docs/对话树.md` §三）----
    "list_conversations": {
        "access": "read",
        "group": "trees",
        "fn": list_conversations,
        "description": (
            "有哪些对话（他和你坐下过的每一段，最近动过的在前）。\n"
            "**它是一张索引**：标题、条数、最后动过的日子，以及哪一条是你们现在正说着的。\n"
            "什么时候用：他说「上次」「之前」「我以前问过」而你不确定是哪一段时；"
            "或者你要去别的对话里找东西、先得知道有哪几棵能翻。\n"
            "拿到 id 之后：看形状用 `read_tree`，取原文用 `read_tree_nodes`。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "最多看几条（默认 20，上限 50）",
                },
            },
        },
    },
    "search_conversations": {
        "access": "read",
        "group": "trees",
        "fn": search_conversations,
        "description": (
            "在**所有对话**的消息正文里搜一段字 —— 「我到底在哪儿学过这个」。\n"
            "命中会给那一条的 snippet（命中点前后各一句），够判断是不是你要找的那句；"
            "接着拿 conversationId + messageId 走 `read_tree_nodes` 取整段。\n"
            "**至少两个字**：一个字（「的」「是」）会命中几乎全部消息，那不是搜索。\n"
            "先搜再答 —— 「我记得没讲过」这种话在搜过之前不该说。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "要搜的一段字（两个字以上）"},
                "limit": {
                    "type": "integer",
                    "description": "最多看几条命中（默认 12，上限 50）",
                },
            },
            "required": ["query"],
        },
    },
    "read_tree": {
        "access": "read",
        "group": "trees",
        "fn": read_tree,
        "description": (
            "读**一棵对话树**的形状：主线怎么走的、在哪儿下钻过、每一支各自有多深。\n"
            "这是他学习方式的现场记录（递归学习法：从讲解里挑出不懂的词、一个个问出去）——"
            "**「我上次是怎么学的 / 那个东西我钻过没有」这类问题，看它比看聊天记录准**。\n"
            "不给 conversationId 就是你现在正说着的这一条。\n"
            "骨架里每条只有预览；要逐字看的，把它行首的 `#id` 交给 `read_tree_nodes`。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "conversationId": {
                    "type": "string",
                    "description": "哪一条对话（缺省 = 当前这条；用 list_conversations 给的 id）",
                },
            },
        },
    },
    "read_tree_nodes": {
        "access": "read",
        "group": "trees",
        "fn": read_tree_nodes,
        "description": (
            "按 id 取某几条消息的**原文** —— `read_tree` 骨架里那些 `#id` 就交给它。\n"
            "为什么要两段式：一次把整棵树展开是骨架的十几倍，而这些原文还会随历史每轮重放。"
            "所以先看形状，再只把需要逐字看的那几条（分叉点的上一轮、他反复问的那几个词）取出来。\n"
            "一次别点太多，通常几条就够。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "conversationId": {
                    "type": "string",
                    "description": "哪一条对话（缺省 = 当前这条）",
                },
                "ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "要读全文的消息 id（骨架里行首的 #号那个数字）",
                },
            },
            "required": ["ids"],
        },
    },
    # ---- 元能力：不属于任何一块（见 META_TOOLS），挂了任何工具时都在 ----
    "run_subagent": {
        "access": "read",
        "group": "",
        "fn": run_subagent,
        "description": (
            "把**一条长工具链**派给子代理：它拿一个任务，在**自己的上下文**里跑完"
            "（查几次、换什么词、几时收手由它自己定），只交回一份结果 ——"
            "中间那些工具调用**一条都不进你的上下文**。\n"
            "**什么时候该用**：你要做的这件事是**串行的多步查询** ——"
            "试一个查询词、零命中、换词再试；把一个概念展开成前置链再逐段找原文；"
            "笔记 / 材料 / 图谱几路都要查一遍再对账 —— 或者中间过程很长而你只要结论。"
            "你自己一步步调也一样能查到，但每一步的原文与试错都会堆进你的上下文，"
            "把真正要紧的东西挤出去。\n"
            "**什么时候别用**：一句话就查得到的事（直接调 search_* 更快）；"
            "以及**要你判断的事** —— 讲什么、判对错、出什么题、怎么评价，"
            "这些留在你这边，别让子代理替你下结论：它只会**找**、只**报**。\n"
            "**它的规矩**：只读（它也没有沙箱，`run_python` 用不了）、不许再派子代理、"
            "冲突要两条都报、查不到要带 `scanned/total` 说清是「真没有」还是「词没给对」。\n"
            "调用形状是**任务描述**，不是工具名清单：说清要达成什么、回来时你想看到什么形状。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": (
                        "任务：要它达成什么、背景是什么、该去哪里找。"
                        "写得像给一个能干的同事派活，而不是给一个搜索框下命令。"
                    ),
                },
                "wants": {
                    "type": "string",
                    "description": (
                        "期望回来的形状（可省）：比如「前置链 + 每级最实的原文段 + 行号」、"
                        "「两条来源的对照表」"
                    ),
                },
            },
            "required": ["task"],
        },
    },
}


def group_of(name: str) -> str:
    """这个工具属于哪一组（没有就空串）。"""
    return str((REGISTRY.get(name) or {}).get("group") or "")


def specs(
    mounts: set[str] | None = None,
    allow: tuple[str, ...] | set[str] | None = None,
) -> list[dict]:
    """OpenAI 兼容的工具声明。

    `mounts` 是这次挂载了哪几组；**`None` = 全都挂上**（没配过的默认）。
    空集合是合法输入，而且正是"极简模式"：一条声明都不给模型，它就只剩聊天。

    `allow` 是这次允许的**权限档**（见 ACCESS_LEVELS）；`None` = 四档全放。
    两个条件是**与**关系：组要对得上，档也要够得着 —— 于是"查询模式"（挂三组、
    只放 read）拿到的是"找笔记 / 读材料 / 走图谱"，一条写操作都不会出现在声明里。
    """
    levels = None if allow is None else set(allow)
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": spec["description"],
                "parameters": spec["parameters"],
            },
        }
        for name, spec in REGISTRY.items()
        if (
            mounts is None
            or str(spec.get("group") or "") in mounts
            # 元能力（`run_subagent`）：不属于任何一块，但"这一版挂了任何工具"时都该在。
            # **极简模式除外** —— 那会儿连查都查不了，派子代理没有意义
            #（空集在这里是假，所以它天然不出现）。
            or (name in META_TOOLS and mounts)
        )
        and (levels is None or str(spec.get("access") or "read") in levels)
    ]


def access_of(name: str) -> str:
    """某个工具的权限档（认不出来的当只读处理）。"""
    return str((REGISTRY.get(name) or {}).get("access") or "read")


def call(
    db,
    name: str,
    args: dict,
    ctx: dict | None = None,
    *,
    mounts: set[str] | None = None,
    allow: tuple[str, ...] | set[str] | None = None,
) -> tuple[bool, dict]:  # noqa: ANN001
    """执行一次工具调用。**永远不抛**：失败也是给模型的一条结果。

    理由见模块 docstring：抛出去的结果是白屏 + 已花的额度，
    而交回一个 {"error": ...} 让模型自己换个问法，往往还能救回来。

    `ctx` 是"这次调用发生在什么场合"（至少含 `conversationId`）：
    有的工具需要它 —— 比如 `push_question` 要知道这次已经推过哪几道。
    """
    spec = REGISTRY.get(name)
    if spec is None:
        return False, {"error": "没有这个工具：" + str(name)}
    # 纵深防御：**没挂载的模块，即使模型把名字报出来也不执行**。
    # 光靠"不声明"是不够的 —— 上下文里可能还留着上一轮的痕迹、
    # 或者模型就是会猜一个名字出来试。
    # 元能力（`run_subagent`）只在"挂了任何工具"时可用 —— 与 `specs` 的放行规则一致：
    # 极简模式里它也不该能调（那会儿连查都查不了，没有活可派）。
    if name in META_TOOLS and mounts is not None and not mounts:
        return False, {
            "error": "这一版没有任何工具（极简模式），没有可派给子代理的活。"
        }
    group = group_of(name)
    if mounts is not None and group and group not in mounts:
        label = GROUP_LABELS.get(group, group)
        return False, {
            "error": f"「{label}」这个模块这次没有挂载，我这边调不动它。"
            f"（顶栏那个图标可以把它亮起来；或者你直接把需要的内容贴给我。）"
        }
    # 纵深防御之二：**这一档没开，报出名字也不执行**。与组那道是同一个道理 ——
    # "只读模式"里模型仍然可能猜到 `grade_problem` 这个名字（上下文里见过、或者就是会猜）。
    if allow is not None:
        level = access_of(name)
        if level not in set(allow):
            return False, {
                "error": "这个工具当前这一版没有开（权限档：" + level + "），我这边调不动它。"
                "需要的话让他把模式切到能改记录的那一档。"
            }
    try:
        # **顺手把"这次挂在什么范围里"也交给工具**：`run_subagent` 要照抄一份给子代理
        #（不放权 —— 见 subagent.py 的说明）。别的工具用不上就放着，不打扰。
        scope = dict(ctx or {})
        scope.setdefault("mounts", mounts)
        scope.setdefault("allow", allow)
        return True, spec["fn"](db, args or {}, scope)
    except Exception as exc:  # noqa: BLE001
        return False, {"error": type(exc).__name__ + ": " + str(exc)[:200]}


def output_text(payload: dict) -> str:
    """工具结果 → 交给模型看的文本（紧凑 JSON，别浪费 token 在缩进上）。

    ## 题卡不原样交出去

    `card` 是给**界面**渲染的：整份塞进上下文等于把题面与四个选项又誊一遍
    （实测一次推题塞进去近千字），而且它还会随历史一次次重放 —— 第二轮请求
    因此涨到 90 秒撞上超时。给模型留一张"身份证"就够了：
    它知道推了哪道题、什么层什么翼，要讲题时自己会去取。
    """
    # 子代理的回报**原样交出去**：那本来就是写给模型看的 Markdown（结论 / 依据 /
    # 原文摘录 / 试过什么）。塞进 JSON 会把它转义成一大团 `\n` 与 `\"` ——
    # 读起来更费劲、还更费 token。
    sub = payload.get("subagent")
    if isinstance(sub, dict) and not payload.get("error"):
        head = str(payload.get("note") or "子代理的回报：")
        report = str(sub.get("report") or "")
        if report:
            return head + "\n\n" + report
        # 没交付：把它最后那句话**另起一段**带上，并写明它**不是**回报 ——
        # 让它挂在 `report` 名下，主线就会拿半句念叨当结论
        #（见 subagent.py 的 `_looks_like_intent`）。
        words = str(sub.get("lastWords") or "")
        if words:
            return head + "\n\n**它最后说的话（不是回报，是干活时的念叨）**：\n" + words
        return head + "\n\n（它没交回正文）"

    # 对话树的骨架同理，而且理由更硬：**它的缩进就是它的意思**（谁从谁那里下钻）。
    # 塞进 JSON 会把每一条的换行与缩进转义成 `\n` 和字面空格，整棵树塌成一行 ——
    # 那正好把它唯一的价值（形状）毁掉。
    tree = payload.get("tree")
    if isinstance(tree, str) and not payload.get("error"):
        head = str(payload.get("note") or "")
        return (head + "\n\n" + tree).strip()

    trimmed = {key: value for key, value in payload.items() if key not in ("card", "draft")}

    # 草稿卡（`create_question`）同理，但**答案要留**：它给用户讲评"为什么选 B"时得看答案；
    # 而整张卡（选项 + 解析）随历史一次次重放纯属白花 token。
    draft = payload.get("draft")
    if isinstance(draft, dict) and draft.get("id"):
        trimmed["draft"] = {
            "id": draft.get("id"),
            "type": draft.get("type"),
            "stem": str(draft.get("stem") or "")[:120],
            "answer": draft.get("answer") or "",
            "note": "（题面与解析已交给界面；讲评时用这里的答案。）",
        }

    card = payload.get("card")
    if isinstance(card, dict) and card.get("questionId"):
        trimmed["card"] = {
            "questionId": card.get("questionId"),
            "type": card.get("type"),
            "layer": card.get("layer"),
            "wing": card.get("wing"),
            "difficulty": card.get("difficulty"),
            "note": "题卡已推给他作答（题面与选项在卡片里，不在你这里）",
        }
    return json.dumps(trimmed, ensure_ascii=False, separators=(",", ":"))
