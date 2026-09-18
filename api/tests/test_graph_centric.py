"""以概念为中心的判边：挑谁去问、以及问过的别重问。

这条路要花钱（一个概念一次调用），所以"挑对了没有"必须能测：
只挑**还没接进有序边**的概念、跳过已经问过的、并且带上共现候选。
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.graph_build import _centric_candidates  # noqa: E402

from app.models import Concept, ConceptEdge  # noqa: E402
from datetime import datetime, timezone  # noqa: E402


def _concept(db, name: str) -> Concept:  # noqa: ANN001
    concept = Concept(
        key="cen-" + uuid.uuid4().hex[:8],
        name=name,
        kind="noun",
        definition=name + "：这是测试用的一句说明，长度要够，免得被那道闸门挡掉。",
        material_count=1,
        point_count=1,
    )
    db.add(concept)
    db.flush()
    return concept


def _co_occurs(db, left: Concept, right: Concept) -> None:  # noqa: ANN001
    db.add(
        ConceptEdge(
            from_concept_id=left.id,
            to_concept_id=right.id,
            type="co_occurs",
            why="测试用",
            derived_by="code",
            weight=1.0,
        )
    )
    db.flush()


def _requires(db, source: Concept, target: Concept) -> None:  # noqa: ANN001
    db.add(
        ConceptEdge(
            from_concept_id=source.id,
            to_concept_id=target.id,
            type="requires",
            why="测试锚定",
            derived_by="llm",
            weight=1.0,
        )
    )
    db.flush()


def _rows() -> list[dict]:
    return _centric_candidates(limit=500)


def test_only_concepts_without_ordered_edges_are_picked(db_session) -> None:  # noqa: ANN001
    lonely = _concept(db_session, "中心-孤")
    peer = _concept(db_session, "中心-邻")
    _co_occurs(db_session, lonely, peer)

    anchored = _concept(db_session, "中心-已接")
    anchor_peer = _concept(db_session, "中心-锚")
    _co_occurs(db_session, anchored, anchor_peer)
    _requires(db_session, anchor_peer, anchored)
    db_session.commit()

    rows = {row["key"]: row for row in _rows()}
    assert lonely.key in rows, "还没接进有序边的概念才是要问的"
    assert anchored.key not in rows, "已经接进图的不再问"
    assert [item["key"] for item in rows[lonely.key]["peers"]] == [peer.key]


def test_a_concept_without_peers_is_skipped(db_session) -> None:  # noqa: ANN001
    """没有共现候选就没法问（模型没东西可挑）—— 跳过，也别标记成问过。"""
    alone = _concept(db_session, "中心-无邻")
    db_session.commit()

    assert alone.key not in {row["key"] for row in _rows()}


def test_what_was_asked_is_not_asked_again(db_session) -> None:  # noqa: ANN001
    """问过就记 `centric_at` —— 模型说"没有前置"的也记，不然同一批反复买。"""
    concept = _concept(db_session, "中心-问过")
    peer = _concept(db_session, "中心-问过邻")
    _co_occurs(db_session, concept, peer)
    concept.centric_at = datetime.now(timezone.utc)
    db_session.commit()

    assert concept.key not in {row["key"] for row in _rows()}
