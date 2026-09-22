"""知识空间与「算法选题」接口。

为什么要有它：题目和知识点一多，靠人工翻文件筛选既选不动也查不动。
这里把两件事交给服务端：

* **检索**：知识点、出处、关系邻域、覆盖缺口 —— 都是查询，不是翻目录。
* **选题（组卷）**：给定主题/层/难度/数量，服务端按算法挑题 —— 而不是让用户
  在一堆筛选器里手挑。选题偏重「该练的」：错过的、到期该复习的、覆盖还很薄的知识点。

读接口全部只要登录、不要 CSRF（与既有的读/写契约一致）。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, select, text

from .. import materials as material_text
from ..deps import DbSession
from ..models import (
    KnowledgePoint,
    Material,
    MyQuestion,
    PointEdge,
    PointSource,
    Question,
    QuestionPoint,
    Record,
)

# 题源是"我的题单"时，一次最多取这么多道进来打分（它是自己攒的题，规模有限）
MAX_SCOPE_QUESTIONS = 500

router = APIRouter(prefix="/api", tags=["knowledge"])

LAYERS = ("识记", "理解", "应用", "迁移")


@router.get("/knowledge/material")
def material_lines(
    db: DbSession,
    slug: str = Query(..., description="材料 slug"),
    start: int = Query(1, ge=1, description="起始行（1 起）"),
    end: int = Query(0, ge=0, description="结束行（含）；0 = 从 start 往下 60 行"),
) -> dict:
    """按行区间读材料原文 —— 对话里点开引用时用它。

    与模型读的是**同一个函数**（`app.materials.read_lines`）：它看到的与你看到的
    逐字一致，这是"引用"能当证据的前提。库只存坐标，正文在文件里，
    所以文件不在机器上时这里会明确说清（而不是返回一段空文本）。
    """
    material = db.scalar(select(Material).where(Material.slug == slug))
    if material is None:
        raise HTTPException(404, "没有这份材料：" + slug)
    try:
        lines = material_text.read_lines(material, start, end)
    except material_text.MaterialError as exc:
        raise HTTPException(503, str(exc)) from None

    return {
        "material": material.slug,
        "title": material.title,
        "sourcePath": material.source_path,
        "totalLines": material.lines,
        "startLine": lines[0]["line"],
        "endLine": lines[-1]["line"],
        "lines": lines,
    }


@router.get("/knowledge/overview")
def overview(db: DbSession) -> dict:
    """知识空间总览：材料、知识点、有题/缺题 —— 缺题清单是「下一步出什么」的唯一依据。"""
    materials = db.execute(select(func.count()).select_from(Material)).scalar_one()
    points = db.execute(select(func.count()).select_from(KnowledgePoint)).scalar_one()
    sources = db.execute(select(func.count()).select_from(PointSource)).scalar_one()
    edges = db.execute(select(func.count()).select_from(PointEdge)).scalar_one()
    covered = db.execute(
        select(func.count(func.distinct(QuestionPoint.point_id)))
    ).scalar_one()
    by_material = db.execute(
        text(
            """
            SELECT m.slug, m.subject, COUNT(DISTINCT p.id) AS points,
                   COUNT(DISTINCT qp.point_id) AS covered
            FROM materials m
            LEFT JOIN knowledge_points p ON p.material_id = m.id
            LEFT JOIN question_points qp ON qp.point_id = p.id
            GROUP BY m.slug, m.subject
            ORDER BY (COUNT(DISTINCT p.id) - COUNT(DISTINCT qp.point_id)) DESC, m.slug
            """
        )
    ).fetchall()
    return {
        "materials": materials,
        "points": points,
        "sources": sources,
        "edges": edges,
        "coveredPoints": covered,
        "gaps": [
            {"material": slug, "subject": subject, "points": pts, "covered": cov, "missing": pts - cov}
            for slug, subject, pts, cov in by_material
            if pts - cov > 0
        ],
    }


@router.get("/knowledge/points")
def search_points(
    db: DbSession,
    q: str = Query("", description="按 key 或名称模糊搜"),
    subject: str = Query(""),
    limit: int = Query(30, ge=1, le=200),
) -> dict:
    """搜知识点（key / 名称）。用于组卷时挑主题，以及查「这个名词在哪份材料里」。"""
    stmt = select(KnowledgePoint, Material.slug).join(Material, Material.id == KnowledgePoint.material_id)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(KnowledgePoint.key.ilike(like) | KnowledgePoint.name.ilike(like))
    if subject:
        stmt = stmt.where(Material.subject == subject)
    rows = db.execute(stmt.order_by(KnowledgePoint.key).limit(limit)).all()
    counts = dict(
        db.execute(
            select(QuestionPoint.point_id, func.count()).group_by(QuestionPoint.point_id)
        ).all()
    )
    return {
        "items": [
            {
                "key": point.key,
                "name": point.name,
                "kind": point.kind,
                "layers": point.layers,
                "thickness": point.thickness,
                "material": slug,
                "questions": counts.get(point.id, 0),
            }
            for point, slug in rows
        ]
    }


@router.get("/knowledge/points/{key}")
def point_detail(key: str, db: DbSession) -> dict:
    """一个知识点的全部：出处行区间、关系邻域、覆盖它的题。

    出处直接给出**行号**：题目讲错时，人（或 AI）能一步翻回原文核对。
    """
    point = db.scalars(select(KnowledgePoint).where(KnowledgePoint.key == key)).first()
    if point is None:
        raise HTTPException(status_code=404, detail=f"没有这个知识点：{key}")
    material = db.get(Material, point.material_id)
    sources = db.scalars(
        select(PointSource).where(PointSource.point_id == point.id).order_by(PointSource.start_line)
    ).all()
    edges = db.execute(
        text(
            """
            SELECT e.type, e.why, p.key AS other
            FROM point_edges e
            JOIN knowledge_points p
              ON p.id = CASE WHEN e.from_point_id = :pid THEN e.to_point_id ELSE e.from_point_id END
            WHERE e.from_point_id = :pid OR e.to_point_id = :pid
            ORDER BY e.type, p.key
            """
        ),
        {"pid": point.id},
    ).fetchall()
    questions = db.execute(
        select(Question.id, Question.type, Question.difficulty)
        .join(QuestionPoint, QuestionPoint.question_id == Question.id)
        .where(QuestionPoint.point_id == point.id)
        .order_by(Question.id)
    ).all()
    return {
        "key": point.key,
        "name": point.name,
        "kind": point.kind,
        "layers": point.layers,
        "thickness": point.thickness,
        "producible": point.producible,
        "material": {"slug": material.slug, "title": material.title, "subject": material.subject}
        if material
        else None,
        "sources": [
            {"material": material.slug if material else "", "start": s.start_line, "end": s.end_line}
            for s in sources
        ],
        "neighbors": [{"type": t, "point": other, "why": why} for t, why, other in edges],
        "questions": [{"id": qid, "type": t, "difficulty": d} for qid, t, d in questions],
    }


@router.get("/picks")
def picks(
    db: DbSession,
    topics: str = Query("", description="逗号分隔的主题/知识点 key，空 = 不限"),
    layers: str = Query("", description="逗号分隔的认知层，空 = 不限"),
    count: int = Query(10, ge=1, le=100),
    mode: str = Query("mix", description="mix=综合 / due=该复习 / weak=薄弱与错题 / gaps=覆盖薄的知识点"),
    scope: str = Query(
        "public",
        description="题源：public=公共题库（默认，保持老行为）/ mine=我的题单 / all=都要",
    ),
) -> dict:
    """**算法选题**：给定条件，服务端挑出最该练的那些题，并说明每道为什么被挑中。

    打分（权重刻意简单、可解释）：
      该复习（SM2 到期或从未练过）  +30
      练错过 / 被标记              +25
      所属知识点出题少（覆盖薄）     +15
      从未接触过的知识点            +10
      同主题去重（同一知识点最多 2 道）
    最后一律按 (分数, 题目 id) 排序 —— 同样的输入给同样的结果，可复现。

    ## 题源（`scope`）

    公共题库与用户题单是**两份东西**（见 `routers/mybank.py` 里为什么不合成一张表），
    但练习时它们是同一个池子里的候选 —— 差别只在"来源"这一列上，
    所以这里按 `scope` 决定把哪些放进池子，并在结果里标出 `source`。

    默认 `public` 是刻意的：老调用方（与老测试）看到的行为一个字都不变。
    """
    topic_list = [t.strip() for t in topics.split(",") if t.strip()]
    layer_list = [l.strip() for l in layers.split(",") if l.strip()]

    # 题 → 点 → 材料，一次读出来在内存里打分（题量与知识点规模都在千级，够用且简单）
    rows = db.execute(
        select(
            Question.id,
            Question.type,
            Question.topic,
            Question.difficulty,
            KnowledgePoint.key,
            KnowledgePoint.layers,
            func.coalesce(func.count(QuestionPoint.question_id), 0).label("nq"),
        )
        .join(QuestionPoint, QuestionPoint.question_id == Question.id, isouter=True)
        .join(KnowledgePoint, KnowledgePoint.id == QuestionPoint.point_id, isouter=True)
        .where(Question.retired_at.is_(None))
        .group_by(Question.id, KnowledgePoint.key, KnowledgePoint.layers)
    ).all()
    points_per_key: dict[str, int] = {}
    for _qid, _t, _topic, _d, pkey, _layers, nq in rows:
        if pkey:
            points_per_key[pkey] = max(points_per_key.get(pkey, 0), int(nq or 0))

    records = {
        r.question_id: r
        for r in db.scalars(select(Record)).all()
    }

    scored: list[tuple[int, str, dict]] = []
    # `scope=mine` 时公共题一道都不进池子 —— 这是"只看我的题单"那句话的全部实现
    for qid, qtype, topic, difficulty, pkey, qlayers, nq in (
        rows if scope in ("public", "all") else []
    ):
        if topic_list and not ({topic, pkey} & set(topic_list)):
            continue
        if layer_list and not (set(layers_of(qlayers)) & set(layer_list)):
            continue
        record = records.get(qid)
        score = 0
        reasons: list[str] = []
        if record is None or record.attempts == 0:
            score += 30
            reasons.append("还没练过")
        elif record.last_status != "correct":
            score += 30
            reasons.append("上次没做对")
        if record is not None and (record.wrong > 0 or record.flagged):
            score += 25
            reasons.append("做错过或被标记")
        thin = points_per_key.get(pkey or "", 0) <= 1
        if thin:
            score += 15
            reasons.append("该知识点题少（覆盖薄）")
        if mode == "due" and record is not None and record.last_at:
            reasons.append("按复习优先级排序")
        scored.append((score, qid, {"id": qid, "type": qtype, "topic": topic,
                                    "difficulty": difficulty, "point": pkey,
                                    "source": "public", "reasons": reasons}))

    # 用户题单进池子：同一套打分，但"覆盖薄"那一条不适用 ——
    # 它本来就不挂知识点、也不参与覆盖率对账（那正是它与公共题分开的原因）。
    if scope in ("mine", "all"):
        for row in db.scalars(
            select(MyQuestion)
            .order_by(MyQuestion.created_at.desc())
            .limit(MAX_SCOPE_QUESTIONS)
        ).all():
            payload = row.payload or {}
            topic = str(payload.get("topic") or "")
            pkey = row.point_key or ""
            if topic_list and not ({topic, pkey} & set(topic_list)):
                continue
            record = records.get(row.id)
            score = 0
            reasons = ["自己攒的题"]
            if record is None or record.attempts == 0:
                score += 30
                reasons.append("还没练过")
            elif record.last_status != "correct":
                score += 30
                reasons.append("上次没做对")
            if record is not None and (record.wrong > 0 or record.flagged):
                score += 25
                reasons.append("做错过或被标记")
            try:
                difficulty = int(payload.get("difficulty") or 3)
            except (TypeError, ValueError):
                difficulty = 3
            scored.append(
                (
                    score,
                    row.id,
                    {
                        "id": row.id,
                        "type": str(payload.get("type") or "single"),
                        "topic": topic,
                        "difficulty": difficulty,
                        "point": pkey,
                        "source": "mine",
                        "reasons": reasons,
                    },
                )
            )

    # 同一知识点最多 2 道：避免一份卷子里全是同一个点
    scored.sort(key=lambda item: (-item[0], item[1]))

    # `scope=all` 时给自己攒的题**留配额**：公共题有上百道，纯按分排序会把题单
    # 整个挤掉 —— 那样"都要"就等于"只要公共"了。留一半（至少一道），
    # 剩下的位置再按分数补公共题与其余的题单题。
    ordered = scored
    if scope == "all":
        mine_rows = [row for row in scored if row[2].get("source") == "mine"]
        reserve = max(1, count // 2) if mine_rows else 0
        head = mine_rows[:reserve]
        reserved = {row[1] for row in head}
        ordered = head + [row for row in scored if row[1] not in reserved]

    picked: list[dict] = []
    per_point: dict[str, int] = {}
    for _score, _qid, item in ordered:
        pkey = item.get("point") or ""
        if pkey and per_point.get(pkey, 0) >= 2:
            continue
        picked.append(item)
        if pkey:
            per_point[pkey] = per_point.get(pkey, 0) + 1
        if len(picked) >= count:
            break
    return {"mode": mode, "count": len(picked), "items": picked}


def layers_of(value) -> list[str]:
    """layers 存在 JSONB 里，这里统一成列表（老数据可能是 None）。"""
    if isinstance(value, list):
        return [str(v) for v in value]
    return []
