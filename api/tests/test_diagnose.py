"""根因诊断：把"这道题错了"翻译成"该补哪个考点、按什么顺序、几步"。

一条链上三层：`base →requires→ mid →requires→ top`，错题的考点是 `top`。
诊断要能走**整条链**（不是只看一层），并且给出起点、步数与每一步的依据。
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from app import graph  # noqa: E402
from app.models import Concept, ConceptEdge, Question, QuestionConcept  # noqa: E402


def _concept(db, name: str) -> Concept:  # noqa: ANN001
    concept = Concept(
        key="dia-" + uuid.uuid4().hex[:8],
        name=name,
        kind="noun",
        definition=name + "：诊断用例的概念。",
        material_count=1,
        point_count=1,
    )
    db.add(concept)
    db.flush()
    return concept


def _requires(db, source: Concept, target: Concept) -> None:  # noqa: ANN001
    db.add(
        ConceptEdge(
            from_concept_id=source.id,
            to_concept_id=target.id,
            type="requires",
            why=f"{source.name} 是 {target.name} 的前置。",
            derived_by="llm",
            weight=1.0,
        )
    )
    db.flush()


def _orphan_question(db) -> str:  # noqa: ANN001
    """挑一道**还没挂概念**的现行题 —— 这样 `wrong` 里就只会多出我挂的那一个。"""
    return db.scalar(
        select(Question.id)
        .where(
            Question.retired_at.is_(None),
            Question.status.in_(("published", "verified")),
            ~select(QuestionConcept.question_id)
            .where(QuestionConcept.question_id == Question.id)
            .exists(),
        )
        .order_by(Question.id)
        .limit(1)
    )


def test_diagnose_walks_the_whole_chain(db_session, imported_bank) -> None:  # noqa: ANN001
    question_id = _orphan_question(db_session)
    assert question_id, "测试库里没有可用的题"
    base = _concept(db_session, "诊断-基")
    mid = _concept(db_session, "诊断-中")
    top = _concept(db_session, "诊断-顶")
    _requires(db_session, base, mid)
    _requires(db_session, mid, top)
    db_session.add(QuestionConcept(question_id=question_id, concept_id=top.id))
    db_session.commit()

    report = graph.diagnose([question_id])

    chain = next(item for item in report["chains"] if item["wrong"]["key"] == top.key)
    keys = [step["key"] for step in chain["steps"]]
    assert keys[-1] == top.key, "链的最后一步必须是错题自己"
    assert keys.index(base.key) < keys.index(mid.key), "越前置的越排在前面"
    assert chain["steps"][0]["isRoot"] is True, "链头是起点"
    assert chain["steps"][0]["why"], "每一步都要带依据（为什么要先学它）"

    assert report["steps"] >= 3
    assert any(root["key"] == base.key for root in report["roots"])
    assert report["order"][-1]["key"] == top.key, "顺序的最后才是错题本身"
    assert report["truncated"] is False


def test_diagnose_says_when_it_stopped_early(db_session, imported_bank) -> None:  # noqa: ANN001
    """撞到深度上限要**说出来** —— 停下来的那个不是"没有前置"，是没走完。"""
    question_id = _orphan_question(db_session)
    assert question_id
    base = _concept(db_session, "诊断-远")
    mid = _concept(db_session, "诊断-近")
    top = _concept(db_session, "诊断-端")
    _requires(db_session, base, mid)
    _requires(db_session, mid, top)
    db_session.add(QuestionConcept(question_id=question_id, concept_id=top.id))
    db_session.commit()

    report = graph.diagnose([question_id], depth=1)
    assert report["truncated"] is True


def test_diagnose_without_questions_is_empty() -> None:
    report = graph.diagnose([])
    assert report["chains"] == [] and report["steps"] == 0 and report["truncated"] is False
