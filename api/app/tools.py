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
import time
import uuid
from pathlib import Path

from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.orm.attributes import flag_modified

from . import mastery, materials
from .models import (
    Concept,
    ConceptEdge,
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


def search_knowledge(db, user, args, ctx=None) -> dict:  # noqa: ANN001
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


def get_point_detail(db, user, args, ctx=None) -> dict:  # noqa: ANN001
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
    preview = search_knowledge(db, user, {"query": key, "limit": 5})
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
    db, point_keys: list[str], limit: int, include_answer: bool, layer: str = ""
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
            "stem": _stem(question),
        }
        if include_answer:
            payload = question.payload or {}
            item["answer"] = str(payload.get("answer") or payload.get("answers") or "")[:300]
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


def get_existing_questions(db, user, args, ctx=None) -> dict:  # noqa: ANN001
    point_key = str(args.get("pointKey") or "").strip()
    layer = str(args.get("layer") or "").strip()
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

    items = _questions_of(db, keys, limit, include_answer, layer=layer)
    return {
        "items": items,
        # 顺带把**题库的形状**报出来：类型 × 层 各有多少。
        # 不加这一项，模型就会拿到的样本去推整体（实测推错成"没有大题"）——
        # 这是可以让数据库直接回答的问题，不该让它猜。
        "bank": _bank_shape(db),
        "note": "answer 默认不给（推题时剧透）；要讲解就把 includeAnswer 设为 true。"
        "items 是**抽样**，要判「题库里有没有某类题」请看 bank（那是全量统计）。",
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


def _bands_by_point(db, user, keys=None):  # noqa: ANN001
    """按知识点算掌握档位 —— `get_mastery` 与图检索**共用**这一份口径。

    为什么必须共用：图检索里"这个前置还没打牢"的判据，与 `get_mastery`
    报出来的那个数字，必须是同一个 —— 否则同一件事在两个工具里给出两种说法，
    而用户只能信一个（他会信那个对他有利的）。

    没答过的点照样返回（`band: new`），因为"这片是空的"本身就是要说的事。
    """
    now = int(time.time() * 1000)
    records = {
        record.question_id: record
        for record in db.scalars(select(Record).where(Record.user_id == user.id)).all()
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


def get_mastery(db, user, args, ctx=None) -> dict:  # noqa: ANN001
    keys = [str(k) for k in (args.get("pointKeys") or []) if str(k).strip()]
    bands = _bands_by_point(db, user, keys or None)
    items = sorted(bands.values(), key=lambda item: (item["score"], item["key"]))
    return {
        "items": items[:40],
        "note": "score 是该点下各题掌握度的平均（0–100）；band 是 new/learning/familiar/mastered 档位。",
    }


def get_due_reviews(db, user, args, ctx=None) -> dict:  # noqa: ANN001
    limit = _clamp(args.get("limit"), 1, 30, 10)
    now = int(time.time() * 1000)

    rows = db.execute(
        select(Record.question_id, Record.patch, Question.layer, Question.type)
        .join(Question, Question.id == Record.question_id)
        .where(Record.user_id == user.id)
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


# 能推成卡片的题型。**大题（problem）不在其中**：它有小问结构与小问级判分，
# 那是另一套界面（刷题页有），塞进一张卡里会变成一个半成品。
CARD_TYPES = ("single", "multi", "blank", "short")


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


def push_question(db, user, args, ctx=None) -> dict:  # noqa: ANN001
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

    stmt = select(Question).where(Question.retired_at.is_(None), Question.type.in_(CARD_TYPES))
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
        for record in db.scalars(select(Record).where(Record.user_id == user.id)).all()
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
# 版本钉死：CDN 上的东西会变，而这份运行时会被拷进 /assets 由我们自己托管。
PYODIDE_VERSION = "0.26.4"
PYODIDE_CDN_BASE = "https://cdn.jsdelivr.net/pyodide/v" + PYODIDE_VERSION + "/full/"
# 本机副本（`make vendor` 取回，`make web` 拷进 api/web/assets/pyodide/）
VENDOR_PYODIDE = Path(__file__).resolve().parents[2] / "vendor" / "pyodide"
SERVED_PYODIDE = Path(__file__).resolve().parents[1] / "web" / "assets" / "pyodide"


def pyodide_base() -> str:
    """运行时从哪取 —— 有本机副本就用本机，没有才退回 CDN。

    为什么优先本机：13MB 的运行时现下，实测在沙箱 iframe 里挂了 60 秒仍不成功
    （用户看到的就是「正在加载运行时」停在那儿）。而且"为了跑一段脚本去联网"
    本身就是件别扭的事 —— 沙箱不该依赖外网。

    `__ORIGIN__` 是给前端填的占位符：沙箱页面里**相对路径解析不了**
    （srcdoc 文档的 base 是 about:srcdoc，`new URL('/assets/…')` 直接抛
    "Invalid URL"），而沙箱里 `location.origin` 是不透明的、页面自己也拼不出来。
    只有宿主知道自己的 origin，所以由它替换。
    """
    for directory in (SERVED_PYODIDE, VENDOR_PYODIDE):
        if (directory / "pyodide.js").is_file():
            return "__ORIGIN__/assets/pyodide/"
    return PYODIDE_CDN_BASE


PYODIDE_BASE = pyodide_base()

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


def _python_page(title: str, code: str, packages: list[str], run_id: str) -> str:
    return (
        _PYODIDE_PAGE.replace("__TITLE__", html.escape(title)[:80])
        .replace("__INDEX__", pyodide_base())
        .replace("__RUNID__", html.escape(run_id)[:40])
        .replace("__PACKAGES__", json.dumps(packages, ensure_ascii=False))
        .replace("__CHECK__", json.dumps(list(packages), ensure_ascii=False))
        .replace("__CODE__", json.dumps(code, ensure_ascii=False))
    )


def run_python(db, user, args, ctx=None) -> dict:  # noqa: ANN001
    """在沙箱里**真跑**一段 Python（Pyodide），结果落在演示面板里。

    ## 为什么要有它

    用户说"跑一段脚本"时，模型不该推辞、也不该让他自己去写 HTML ——
    它只要把 Python 交出来，Pyodide 的样板由这里负责。

    ## 能跑什么、跑不了什么

    * 标准库、纯计算、文本输出：没问题。
    * `numpy` / `scipy`：**本机已经装好**（连同 openblas），`packages` 里点名即可，
      不用联网、不用等下载。
    * **跑不了**：要编译或依赖系统库的（`torch`、某些 `pandas` 依赖）、
      要读本机文件或连数据库的 —— 沙箱里没有文件系统，也没有用户的数据。
    * **没有 matplotlib**：要画图得先接 canvas，这里没接。要图就用 JS 画，
      或者把数据 print 出来。

    ## 你看不到输出（这条最要紧）

    输出落在面板上，**不会回到你的上下文里**。所以：

    * 别说"跑出来了，结果是 X" —— 你没看到，那就是编的（实测发生过：
      声称 numpy 可用，而面板上写着 No module named 'numpy'）。
    * 说"我写了一段验证 XX 的代码，跑一下"就够。输出会**自动回填**到那条消息上
      （界面直接显示），并且在你**下一轮**说话时进你的上下文 —— 那时再下结论。
      不要让他复制粘贴，也不要问他"结果是什么"。
    """
    code = str(args.get("code") or "").strip()
    if not code:
        return {"error": "code 不能为空（给我要跑的 Python 源码）。"}
    if len(code) > CODE_MAX_CHARS:
        return {
            "error": f"代码太长（{len(code)} 字，上限 {CODE_MAX_CHARS}）—— 精简到能说明问题就行。"
        }

    asked = [str(item).strip() for item in (args.get("packages") or []) if str(item).strip()][:4]
    # 装什么：numpy 总是装（12MB，几乎每段脚本都用）；scipy 是 47MB ——
    # **代码里真的提到它**（或模型点名）时才装。常见的"纯计算 + numpy"因此
    # 不必先等 63MB。代价是动态导入（`__import__("scipy")`）会被漏掉 ——
    # 这条写在工具说明里了。
    hint = " ".join([code] + asked).lower()
    packages = [name for name in PYODIDE_PACKAGES if name == "numpy" or name in hint]

    # 名单外的剔掉并告诉模型。Pyodide 能不能装某个包是**写死的事实**，猜不出来，
    # 而猜错的代价是"看起来装上了、跑起来 No module named"（实测发生过）。
    skipped: list[str] = []
    for name in asked:
        key = name.lower().strip().split(".")[0]  # scipy.signal 这种也认成 scipy
        if key and key not in PYODIDE_PACKAGES and key not in skipped:
            skipped.append(key)

    title = str(args.get("title") or "").strip()[:80] or "Python 运行结果"
    run_id = uuid.uuid4().hex[:12]
    note = (
        "代码已经挂上，面板里真跑。**不要**把源码贴进正文，"
        "**也不要**声称你已经看到输出 —— 你此刻看不到，它跑在沙箱里。"
        "正文里说清这段在验证什么就够了；跑完的输出会**自动回填到那条消息上**，"
        "而且**在你下一轮说话时进你的上下文** —— 所以不必让他复制粘贴，"
        "也不必问他结果是什么。"
        "沙箱固定带这几个包：" + "、".join(PYODIDE_PACKAGES) + "（不必再点名，"
        "面板开头会报出实际版本）。numpy 每次都装，scipy 只在代码里出现它时才装 —— "
        "**用 `__import__(\"scipy\")` 这种动态写法时请在 packages 里点名**。"
    )
    if skipped:
        note += (
            " 你要的 " + "、".join(skipped) + " 不在这个子集里，**没有装** —— "
            "换名单里的包，或者用标准库自己实现；也可以如实告诉他这个沙箱装不了。"
        )
    return {
        "demo": {
            "title": title,
            "html": _python_page(title, code, packages, run_id),
            "runId": run_id,
        },
        "note": note,
    }


# ---------------------------------------------------------------- 演示沙箱


# 演示 HTML 的上限：这份东西会**落进零件的库**、每次读会话都发给前端，
# 所以它得小。80KB 足够画一个像样的数据流/切分/流水线可视化。
DEMO_MAX_CHARS = 80_000

# 演示套件（React + JSX + d3 + 我们那层组件）由服务端统一注入，见 `_demo_page`。
# 顺序有讲究：经典脚本按出现顺序执行 —— Tailwind 先（它的预置样式要被 kit CSS 盖住）、
# React/ReactDOM 在 kit 之前、Babel 在模型的 `text/babel` 之前。
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
PYODIDE_PACKAGES = ("numpy", "scipy")
PYODIDE_PACKAGE_URL = {"numpy": "https://pyodide.org/en/stable/usage/packages-in-pyodide.html"}


def _demo_kit_ready() -> bool:
    """套件是否已就位（`make vendor` 取回、构建拷进 /assets/demo-kit/）。"""
    for directory in (DEMO_SERVED_KIT, DEMO_VENDOR_KIT):
        if (directory / "react.js").is_file() and (directory / "qf-kit.js").is_file():
            return True
    return False


def _demo_page(page: str, title: str) -> str:
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
    head = ['<link rel="stylesheet" href="' + DEMO_KIT_DIR + 'qf-kit.css">']
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
        '<!doctype html>\n<html lang="zh">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>" + html.escape(title) + "</title>\n" + block + "\n</head>\n<body>\n"
        + page
        + "\n</body>\n</html>"
    )


def render_demo(db, user, args, ctx=None) -> dict:  # noqa: ANN001
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

    if not _demo_kit_ready():
        # 套件没同步就退回老规矩：让模型自己引库（能联网时可用）
        return {
            "demo": {"title": title[:80], "html": page},
            "note": "演示已挂上（沙箱 iframe，允许联网）。套件未就绪（`make vendor` 可取回），"
            "所以库里得自己引 —— 建议直接用 d3 这类 https CDN。",
        }

    return {
        "demo": {"title": title[:80], "html": _demo_page(page, title)},
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


def explore_graph(db, user, args, ctx=None) -> dict:  # noqa: ANN001
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
        db, user, [key for keys in keys_by_concept.values() for key in keys] or None
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


def search_material(db, user, args, ctx=None) -> dict:  # noqa: ANN001
    """在材料**原文**里做字面检索 —— 不是概念库，是正文本身。

    ## 三条使用规则（也是给模型的）

    * 词给**特别的**：`exp_approx_mode`、`circular buffer` 这种原词最灵；
      给"地址""性能"这种到处都有的词，等于没筛。
    * 所有词要在**同一行**同时出现才算命中（词之间是"与"）。放宽就少给一个词。
    * 返回里带 `scanned` / `total`：没扫完就是没扫完，别当成"全库没有"。

    ## 与 `search_knowledge` 的分工

    `search_knowledge` 查的是**知识空间**（概念、点、题）；这个查的是**文本**。
    想知道"材料里原话怎么说的"，用这个；想知道"这个点在图谱里的位置"，用那个。
    """
    result = materials.search(
        db,
        str(args.get("query") or ""),
        slug=str(args.get("slug") or "").strip(),
        limit=_clamp(args.get("limit"), 1, 8, 5),
    )

    # `sources` 是给宿主看的约定：router 会把它变成**引用零件**，
    # 于是对话里每一处结论都能点回原文那几行（不只是"我看过材料"）。其他
    # 工具（search_knowledge / get_point_detail）也照这个字段名返回。
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
    return result


def read_material(db, user, args, ctx=None) -> dict:  # noqa: ANN001
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


def grade_problem(db, user, args, ctx=None) -> dict:  # noqa: ANN001
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
    record = db.scalar(
        select(Record).where(Record.user_id == user.id, Record.question_id == question.id)
    )
    if record is None:
        record = Record(
            user_id=user.id,
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


def _question_proposal(db, user, question_id, kind: str, on) -> dict:  # noqa: ANN001
    """组装一张凭条。**只读不写** —— 这是它存在的全部意义。"""
    question_id = str(question_id or "").strip()
    if not question_id:
        return {"error": "要告诉我哪道题（questionId）。"}
    question = db.get(Question, question_id)
    if question is None or question.retired_at is not None:
        return {"error": "题库里没有这道题：" + question_id}

    field = _ACTION_FIELDS[kind]
    record = db.scalar(
        select(Record).where(Record.user_id == user.id, Record.question_id == question_id)
    )
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


def flag_question(db, user, args, ctx=None) -> dict:  # noqa: ANN001
    """提案：把某道题加入收藏夹 / 移出收藏夹。**不改任何东西**。

    ## 为什么是提案而不是直接写

    它写的是**用户的学习记录**，那是他的东西。AI 可以建议"这题值得留着"，
    但按下去的那一下得是他自己 —— 界面上会出现一张凭条，
    他点确认才落到记录里（走的是收藏夹那条老路）。
    """
    return _question_proposal(db, user, args.get("questionId"), "flag", args.get("on", True))


def mark_mastered(db, user, args, ctx=None) -> dict:  # noqa: ANN001
    """提案：把某道题标成「已掌握」（错题本不再催它）/ 取消这个标记。**不改任何东西**。

    这道题的作答记录**不会**因此变化 —— 标记掌握是"别再催我了"，
    不是"我答对了"。掌握度仍然只从作答长出来。
    """
    return _question_proposal(db, user, args.get("questionId"), "mastered", args.get("on", True))


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


def create_question(db, user, args, ctx=None) -> dict:  # noqa: ANN001
    """自己出一道题 —— **临时题**：先只留在对话里，用户按「存进题单」才落库。

    ## 为什么要这个工具

    题库是按知识点预先出好的（`push_question` 从里面挑）。但真实的教学里有一半是
    "就着刚才这段话，我给你编一道" —— 材料里没有现成的、或用户想要个更贴他错法的。
    没有这个工具时，模型只能推题库里的题，于是常常"讲得很好、一到练就离题"。

    ## 临时题与题库的关系

    * **默认不落库**：返回的是一张草稿卡，用户看着它作答；他觉得值得留，按「存进题单」
      才写进 `user_questions`（与公共题库分开，见 `routers/mybank.py`）。
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


REGISTRY = {
    "search_knowledge": {
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
        "fn": get_existing_questions,
        "description": "题库里已有的题（默认不含答案，避免推题时剧透）。"
        "用户想练某个知识点、或你想看看已有题长什么样时用它。",
        "parameters": {
            "type": "object",
            "properties": {
                "pointKey": {"type": "string", "description": "限定某个知识点的 key，可省略"},
                "layer": {"type": "string", "description": "识记 / 理解 / 应用 / 迁移，可省略"},
                "limit": {"type": "integer", "description": "最多几道，默认 5"},
                "includeAnswer": {"type": "boolean", "description": "讲解时设为 true"},
            },
        },
    },
    "get_mastery": {
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
        "fn": get_due_reviews,
        "description": "按间隔重复算法到期该复习的题。用户问「今天该复习什么」时用它。",
        "parameters": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "最多几条，默认 10"}},
        },
    },
    "run_python": {
        "fn": run_python,
        "description": "在沙箱里**真跑**一段 Python（Pyodide：浏览器里跑 CPython）。"
        "用户说「跑一段脚本」「算一下」「验证一下这个算法」「试试这段代码」时**直接用它**，"
        "不要推辞、也不要让他自己去写页面。只给核心逻辑，用 print 出结果 —— "
        "样板（加载运行时、接 stdout、显示报错）由工具负责。"
        "标准库 + **固定一个子集：numpy 与 scipy**（本机现成，不必点名，"
        "面板开头会报出实际版本）；别的装不了 —— 没有 pandas、没有 matplotlib"
        "（要图就用 render_demo 写 JS）、没有文件系统与网络。\n"
        "面板底部会把时间分成三段报出来（运行时 / 依赖 / 代码）。"
        "**scipy 的子模块（signal、optimize 这些）第一次 import 要 1–2 秒** —— "
        "那是它的固有成本，不是沙箱慢；跟他说清楚，别让他以为是环境有问题。\n"
        "**你看不到运行输出** —— 它落在面板上（沙箱在 iframe 里，输出不进你的上下文）。"
        "所以不要说「跑出来了，结果是 X」：那是编的，实测发生过（声称 numpy 可用，"
        "面板上却是 No module named 'numpy'）。"
        "你只需说清写这段在验证什么。**输出会自动回填到那条消息上**（界面上直接显示），"
        "并且**在你下一轮说话时进你的上下文** —— 所以不要让他复制粘贴、也不要问他"
        "「结果是什么」，下一条消息你自己就能看到。",
        "parameters": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "要跑的 Python 源码（用 print 输出）"},
                "title": {"type": "string", "description": "一句话说明这段在算什么"},
                "packages": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "一般不用填（numpy / scipy 默认就装好了）。"
                    "只有确实需要额外包时才列，且必须在名单内 —— 名单外的会被剔除并告知。",
                },
            },
            "required": ["code"],
        },
    },
    "render_demo": {
        "fn": render_demo,
        "description": "产出一个**能动的演示**（跑在沙箱 iframe 里）。"
        "机制里有空间/时间结构时用它：数据怎么流、怎么切、怎么重叠、流水线怎么排、瓶颈在哪 —— "
        "一张能动的图胜过三段文字。**不要**用它讲定义、结论或代码逐行解释；要跑 Python 用 run_python。\n"
        "**沙箱里已经备好一套前端套件，由服务端自动注入 —— 你不要写任何 `<script src>`，"
        "也不要引 CDN。** 可用的是：React 18 + JSX（写在 `<script type=\"text/babel\">` 里）、"
        "htm、d3 v7、Tailwind，以及 `window.QFKit`：\n"
        "* `QFKit.mount(<App/>)` —— 一行挂载\n"
        "* `QFKit.Frame / Card / Row / Btn / Legend / KV / Note / Chip` —— 布局与信息块\n"
        "* `QFKit.useTicker(speed, {paused})` —— 帧驱动步进（**不要**用 setInterval 改 DOM）\n"
        "* `QFKit.scales({width,height,xDomain,yDomain})` + `QFKit.useAxis(ref,{scales})` —— "
        "坐标轴与数据**共用同一组比例尺**（手画坐标轴十次九次是歪的）\n"
        "* `QFKit.colors` —— 色板（`series` 给多条序列）\n"
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
        "fn": search_material,
        "description": "在**材料原文**里做字面检索，返回命中的行区间与原文片段。"
        "想知道「材料里原话是怎么说的」时用它；想知道概念/点在知识空间里的位置，用 search_knowledge。"
        "词要给得特别（exp_approx_mode、circular buffer 这种原词最灵）—— "
        "所有词要在**同一行**同时出现才算命中，放宽就少给一个词。"
        "返回里的 scanned/total 说明这次扫了多少份材料：没扫完就别说「材料里没有」。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "关键词或短语（按字面命中）"},
                "slug": {"type": "string", "description": "只在这份材料里找（可省略）"},
                "limit": {"type": "integer", "description": "最多几段，默认 5"},
            },
            "required": ["query"],
        },
    },
    "read_material": {
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
        "fn": push_question,
        "description": "推一道题给他做（返回一张**可作答的题卡**，不含答案）。"
        "他说「考考我」「来道题」「练一道」时用它；你刚讲完一段机制、想确认他确实懂了时也可以主动推。"
        "题卡由界面渲染、他答完结果会自动进答题记录 —— 所以**不要**报答案，也不要替他念选项。"
        "**大题（problem）不在这张卡里** —— 它有多问、要写推导，由界面上的「大题」"
        "子窗口负责（那边有专职的子代理批改，题库里也确实有大题）。"
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
                "maxDifficulty": {"type": "integer", "description": "难度上限 1–5"},
            },
        },
    },
    "create_question": {
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
}


def specs() -> list[dict]:
    """OpenAI 兼容的工具声明。"""
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
    ]


def call(db, user, name: str, args: dict, ctx: dict | None = None) -> tuple[bool, dict]:  # noqa: ANN001
    """执行一次工具调用。**永远不抛**：失败也是给模型的一条结果。

    理由见模块 docstring：抛出去的结果是白屏 + 已花的额度，
    而交回一个 {"error": ...} 让模型自己换个问法，往往还能救回来。

    `ctx` 是"这次调用发生在什么场合"（至少含 `conversationId`）：
    有的工具需要它 —— 比如 `push_question` 要知道这次已经推过哪几道。
    """
    spec = REGISTRY.get(name)
    if spec is None:
        return False, {"error": "没有这个工具：" + str(name)}
    try:
        return True, spec["fn"](db, user, args or {}, ctx or {})
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
