"""覆盖率对账里的那条硬断言：**游离题必须为零**。

游离题 = 现行题里一条概念边都没有的。它们不报错，只是安静地掉出所有算法
（覆盖率算不到、按知识点选题选不到、掌握度回流不到图上）—— 实测曾经有 86 道。
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline import coverage  # noqa: E402

from app.models import Concept, QuestionConcept  # noqa: E402


def test_a_question_without_any_concept_is_an_orphan(db_session, imported_bank) -> None:  # noqa: ANN001
    """口径就一句话：**一条概念边都没有**。挂上一条就不再算。"""
    stray = coverage.orphans(limit=1)
    assert stray, "测试库里连一道游离题都没有？那这条用例失去了意义"
    question_id = stray[0]["id"]
    assert question_id in {row["id"] for row in coverage.orphans(limit=0)}

    concept = Concept(
        key="cov-" + uuid.uuid4().hex[:8],
        name="覆盖率用例的概念",
        kind="noun",
        definition="给游离题挂一个概念，用来验证它不再游离。",
        material_count=1,
        point_count=1,
    )
    db_session.add(concept)
    db_session.flush()
    db_session.add(QuestionConcept(question_id=question_id, concept_id=concept.id))
    db_session.commit()

    assert question_id not in {row["id"] for row in coverage.orphans(limit=0)}


def test_orphans_carry_enough_to_find_them(db_session) -> None:  # noqa: ANN001
    """报出来的每条要能定位：id + 考纲 + 层 + 题干开头（不然人不知道去补哪一道）。"""
    for row in coverage.orphans(limit=3):
        assert row["id"]
        assert "topic" in row and "layer" in row and "stem" in row
        assert len(row["stem"]) <= 40
