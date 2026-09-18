"""判边的**候选挑选**：问哪一对、按什么顺序、重跑会不会重复买同一个答案。

为什么单独测这个：判边要花钱（每条一次模型调用），而"钱花在哪一对上"和
"重跑会不会白花"全在候选查询里 —— 这两件事不该等到账单上才发现。
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.graph_build import JUDGED_NONE, _relate_candidates  # noqa: E402

from app.models import Concept, ConceptEdge  # noqa: E402


def _concept(db, name: str) -> Concept:  # noqa: ANN001
    """造一个概念。说明刻意写长 —— 候选里有一道"说明太短不问"的闸门（见 `MIN_DEFINITION_CHARS`）。"""
    concept = Concept(
        key="rel-" + uuid.uuid4().hex[:8],
        name=name,
        kind="noun",
        definition=name + "：这是测试用的一句说明，长度要够，免得被那道闸门挡掉。",
        material_count=1,
        point_count=1,
    )
    db.add(concept)
    db.flush()
    return concept


def _co_occurs(db, left: Concept, right: Concept, weight: float = 1.0, derived_by: str = "code"):  # noqa: ANN001
    edge = ConceptEdge(
        from_concept_id=left.id,
        to_concept_id=right.id,
        type="co_occurs",
        why="测试用",
        derived_by=derived_by,
        weight=weight,
    )
    db.add(edge)
    db.flush()
    return edge


def _anchor(db, concept: Concept) -> None:  # noqa: ANN001
    """给概念接一条有序边 —— 表示"它已经进图了"。"""
    other = _concept(db, concept.name + "-锚")
    db.add(
        ConceptEdge(
            from_concept_id=concept.id,
            to_concept_id=other.id,
            type="requires",
            why="测试锚定",
            derived_by="llm",
            weight=1.0,
        )
    )
    db.flush()


def test_what_was_judged_as_none_is_not_asked_again(db_session) -> None:  # noqa: ANN001
    """判过"没有关系"的边必须退出候选 —— 不然分批跑就是反复买同一个答案。"""
    left = _concept(db_session, "rel-甲")
    right = _concept(db_session, "rel-乙")
    judged = _co_occurs(db_session, left, right, derived_by=JUDGED_NONE)
    db_session.commit()

    rows = _relate_candidates(limit=50, min_weight=1.0)
    assert all(row["edge_id"] != judged.id for row in rows)


def test_unanchored_pairs_are_asked_first(db_session) -> None:  # noqa: ANN001
    """两端都已经接进有序边的、权重再高也排在后面 —— 补边的瓶颈是孤点。

    一个孤点的**第一条**前置边才是把路径接起来的那条；两端都在图里的对，
    判出来只是锦上添花。所以排序先看有序度，同档才看 weight。
    """
    anchored_left = _concept(db_session, "rel-已锚甲")
    anchored_right = _concept(db_session, "rel-已锚乙")
    _anchor(db_session, anchored_left)
    _anchor(db_session, anchored_right)
    anchored = _co_occurs(db_session, anchored_left, anchored_right, weight=9.0)

    lonely_left = _concept(db_session, "rel-孤甲")
    lonely_right = _concept(db_session, "rel-孤乙")
    lonely = _co_occurs(db_session, lonely_left, lonely_right, weight=1.0)
    db_session.commit()

    ids = [row["edge_id"] for row in _relate_candidates(limit=50, min_weight=1.0)]
    assert lonely.id in ids and anchored.id in ids
    assert ids.index(lonely.id) < ids.index(anchored.id)


def test_min_weight_still_gates_the_batch(db_session) -> None:  # noqa: ANN001
    """`--min-weight` 仍然管用：它是"证据够不够硬"的闸门（现网 2271 条是 weight=1）。"""
    left = _concept(db_session, "rel-轻甲")
    right = _concept(db_session, "rel-轻乙")
    light = _co_occurs(db_session, left, right, weight=1.0)
    db_session.commit()

    rows = _relate_candidates(limit=50, min_weight=2.0)
    assert all(row["edge_id"] != light.id for row in rows)


def test_a_pair_that_already_has_a_relation_is_not_asked_again(db_session) -> None:  # noqa: ANN001
    """这一对已经买过答案了（有语义边）就不再问 —— 共现边不该把它捞回池子。

    实测踩过：`build_edges` 先删 `derived_by='code'` 再重插共现边，一次
    `make graph` 就把 1152 对已判的配对复活成"待判"。
    """
    left = _concept(db_session, "rel-答甲")
    right = _concept(db_session, "rel-答乙")
    edge = _co_occurs(db_session, left, right)
    db_session.add(
        ConceptEdge(
            from_concept_id=left.id,
            to_concept_id=right.id,
            type="requires",
            why="判过了",
            derived_by="llm",
            weight=1.0,
        )
    )
    db_session.commit()

    rows = _relate_candidates(limit=200, min_weight=1.0)
    assert all(row["edge_id"] != edge.id for row in rows)


def test_a_barely_defined_concept_is_not_worth_a_call(db_session) -> None:  # noqa: ANN001
    """说明太短的概念不问 —— 那是抽取留下的噪声点，问了多半只换来一个 `none`。"""
    good_left = _concept(db_session, "rel-好甲")
    good_right = _concept(db_session, "rel-好乙")
    good = _co_occurs(db_session, good_left, good_right)

    thin = _concept(db_session, "rel-薄")
    thin.definition = "薄"
    thin_other = _concept(db_session, "rel-薄乙")
    bad = _co_occurs(db_session, thin, thin_other)
    db_session.commit()

    rows = _relate_candidates(limit=200, min_weight=1.0)
    ids = [row["edge_id"] for row in rows]
    assert bad.id not in ids
    assert good.id in ids
