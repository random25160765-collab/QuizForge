"""图谱体检：把 cc.md 那句"图上任取概念能走到根、无环、可传递闭包不矛盾"钉成断言。

两条层次：

* ``_find_cycles`` 是**纯函数**（DFS），拿假数据把"认不认得出环"测干净；
* ``check`` 要连库 —— 所以造一小撮概念与边，再让体检在**整张图**上跑，
  只要它报出的问题里有我造的这几个 key，就说明检测逻辑真在图上工作。

为什么环是这里最要紧的一条：环等价于"沿前置链往上走**永远到不了根**"，
而"先学哪个"这件事的全部价值都吊在前置链上。
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.graph_build import _find_cycles, check  # noqa: E402

from app.models import Concept, ConceptEdge  # noqa: E402


def _concept(db, name: str) -> Concept:  # noqa: ANN001
    """造一个概念（key 唯一：测试库是整个会话共用的）。"""
    concept = Concept(
        key="cyc-" + uuid.uuid4().hex[:8],
        name=name,
        kind="noun",
        definition=name + "的定义。",
        material_count=1,
        point_count=1,
    )
    db.add(concept)
    db.flush()
    return concept


def _edge(db, src: Concept, dst: Concept, kind: str) -> None:  # noqa: ANN001
    db.add(
        ConceptEdge(
            from_concept_id=src.id,
            to_concept_id=dst.id,
            type=kind,
            why="测试造边",
            derived_by="llm",
            weight=1.0,
        )
    )
    db.flush()


# ------------------------------------------------------------------ 纯函数


def test_a_dag_has_no_cycle() -> None:
    assert _find_cycles([("a", "b"), ("b", "c"), ("a", "c")]) == []
    assert _find_cycles([]) == []


def test_a_ring_is_found_and_reported_as_a_path() -> None:
    """报出来的必须是**路径**：只说"有环"人修不动。"""
    cycles = _find_cycles([("a", "b"), ("b", "c"), ("c", "a")])
    assert len(cycles) == 1
    assert cycles[0][0] == cycles[0][-1], "环的写法是首尾同一个节点"
    assert set(cycles[0]) == {"a", "b", "c"}


def test_a_self_loop_is_a_cycle_of_one() -> None:
    assert _find_cycles([("a", "a")]) == [["a", "a"]]


def test_a_ring_is_reported_once_not_once_per_node() -> None:
    """三色 DFS 的收益：同一个环不该按入口数报三遍。"""
    assert len(_find_cycles([("a", "b"), ("b", "c"), ("c", "a")])) == 1


# ------------------------------------------------------------------ 连库


def test_check_reports_a_pre_req_cycle(db_session) -> None:  # noqa: ANN001
    """`A→requires→B→requires→C→requires→A`：三个人互为前置，排不出先后。"""
    tag = "cyc-" + uuid.uuid4().hex[:6]
    nodes = [_concept(db_session, f"{tag}-{name}") for name in ("a", "b", "c")]
    for index in range(3):
        _edge(db_session, nodes[index], nodes[(index + 1) % 3], "requires")
    db_session.commit()

    report = check(limit=50)
    cycles = next(problem for problem in report["problems"] if problem["name"] == "cycles")
    assert any(tag in sample for sample in cycles["samples"]), cycles["samples"]
    assert report["ok"] is False, "有环就必须判死 —— 它是闸门"
    assert report["metrics"]["concepts"] >= 3


def test_check_reports_both_relation_kinds_between_one_pair(db_session) -> None:  # noqa: ANN001
    """同一对概念既"是前提"又"是一部分"：只能留一句更准的。"""
    tag = "cyc-" + uuid.uuid4().hex[:6]
    left = _concept(db_session, f"{tag}-左")
    right = _concept(db_session, f"{tag}-右")
    _edge(db_session, left, right, "requires")
    _edge(db_session, left, right, "part_of")
    db_session.commit()

    report = check(limit=50)
    conflicts = next(
        problem for problem in report["problems"] if problem["name"] == "kind_conflicts"
    )
    assert any(tag in sample for sample in conflicts["samples"]), conflicts["samples"]


def test_a_clean_pair_is_not_flagged(db_session) -> None:  # noqa: ANN001
    """干净的一条前置边不该被报 —— 否则体检会天天喊狼来了。"""
    tag = "cyc-" + uuid.uuid4().hex[:6]
    basis = _concept(db_session, f"{tag}-前置")
    advanced = _concept(db_session, f"{tag}-后继")
    _edge(db_session, basis, advanced, "requires")
    db_session.commit()

    report = check(limit=50)
    for problem in report["problems"]:
        assert not any(tag in sample for sample in problem["samples"]), problem


def test_check_measures_the_filling_progress(db_session) -> None:  # noqa: ANN001
    """`unrooted` 是补边的进度尺：新造的概念**一条有序边都没有**时要算进去。

    它的意思是"这个点上任何路径推荐只会说没有前置，而这句话没法验证" ——
    不是错，是缺口。所以它只报不判死。
    """
    tag = "cyc-" + uuid.uuid4().hex[:6]
    _concept(db_session, f"{tag}-孤点")
    db_session.commit()

    report = check(limit=5)
    assert report["metrics"]["unrooted"] >= 1
    assert report["metrics"]["concepts"] >= report["metrics"]["concepts_with_ordered_edge"]
    assert "ordered_per_concept" in report["metrics"]
