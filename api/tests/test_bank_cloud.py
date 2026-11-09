"""题库整包同步：业务键对齐、幂等、以及"空库不推"的安全阀。

这套用例守着的事很简单，但每一条都真的会出事：

* 两台机器上 `concepts.id` / `knowledge_points.id` 是各自自增的，**不能**按 id 搬；
* 点挂在 `materials.slug` 上，材料没到就写不进去（要跳过、下一轮再补，不能报错）；
* 本机一道题都没有时推空包 = 抹掉云端题库。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import Session, sessionmaker

from app import bank_cloud, sync
from app.db import Base
from app.models import (
    Concept,
    ConceptEdge,
    KnowledgePoint,
    Material,
    PointEdge,
    PointSource,
    Question,
    QuestionConcept,
    QuestionPoint,
    Topic,
)


def _machine(tmp_path, name: str):  # noqa: ANN001, ANN202
    engine = create_engine(
        f"sqlite:///{tmp_path / name}.db", connect_args={"check_same_thread": False}
    )

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _record) -> None:  # noqa: ANN001
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    sync.ensure_sync_schema(engine)
    return engine


def _session(engine) -> Session:  # noqa: ANN001
    return sessionmaker(bind=engine)()


def _seed(engine) -> None:  # noqa: ANN001
    """一份最小的题库：主题 / 概念 / 两个点 / 出处 / 两种边 / 一道题。"""
    with _session(engine) as db:
        material = Material(slug="m-one", subject="信号", title="材料一")
        db.add(material)
        db.flush()

        db.add(Topic(key="t1", name="主题一"))
        concept = Concept(key="c-one", name="概念一")
        db.add(concept)
        db.flush()

        first = KnowledgePoint(
            material_id=material.id, key="p-one", name="点一", concept_id=concept.id
        )
        second = KnowledgePoint(material_id=material.id, key="p-two", name="点二")
        db.add_all([first, second])
        db.flush()

        db.add(PointSource(point_id=first.id, slice_id="s1", start_line=1, end_line=5))
        db.add(PointEdge(from_point_id=first.id, to_point_id=second.id, type="requires"))
        db.add(ConceptEdge(from_concept_id=concept.id, to_concept_id=concept.id, type="requires"))

        db.add(
            Question(
                id="q-one",
                type="short",
                topic="t1",
                payload={"stem": "问题", "answer": "答案"},
                content_hash="h-one",
                raw_markdown="# 问题",
            )
        )
        db.add(QuestionConcept(question_id="q-one", concept_id=concept.id))
        db.add(QuestionPoint(question_id="q-one", point_id=first.id))
        db.commit()


def test_pull_restores_the_graph_by_business_keys(tmp_path, remote) -> None:  # noqa: ANN001
    """A 推、B 拉：题目 id 不变，点与边落到 B 自己的自增 id 上。"""
    machine_a = _machine(tmp_path, "a")
    machine_b = _machine(tmp_path, "b")
    _seed(machine_a)

    # B 上先有一份别的材料与点，把自增 id 顶偏 —— 这正是"不能按 id 搬"的地方。
    # 同时 `m-one` 按 slug 已经在了（`library_cloud` 先拉过材料的样子），只是 id 不同。
    with _session(machine_b) as db:
        other = Material(slug="m-other", subject="别的", title="占位")
        db.add(other)
        db.flush()
        db.add(KnowledgePoint(material_id=other.id, key="p-other", name="占位点"))
        db.add(Material(slug="m-one", subject="信号", title="材料一"))
        db.commit()

    with _session(machine_a) as db:
        bank_cloud.push(db, remote=remote)
    with _session(machine_a) as db:
        source_point_id = db.scalar(select(KnowledgePoint.id).where(KnowledgePoint.key == "p-one"))

    with _session(machine_b) as db:
        result = bank_cloud.pull(db, remote=remote)

    assert result["questions"] == 1
    assert result["concepts"] == 1
    assert result["skippedPoints"] == 0

    with _session(machine_b) as db:
        assert db.scalar(select(Question).where(Question.id == "q-one")) is not None

        material = db.scalar(select(Material).where(Material.slug == "m-one"))
        points = {
            p.key: p
            for p in db.scalars(select(KnowledgePoint).where(KnowledgePoint.material_id == material.id))
        }
        assert set(points) == {"p-one", "p-two"}
        assert points["p-one"].concept_id == db.scalar(select(Concept.id))

        # 业务键对齐的证据：同一个点，两台机器上的自增 id 不一样
        assert points["p-one"].id != source_point_id

        source = db.scalar(select(PointSource).where(PointSource.point_id == points["p-one"].id))
        assert source is not None and (source.start_line, source.end_line) == (1, 5)

        edge = db.scalar(select(PointEdge))
        assert (edge.from_point_id, edge.to_point_id) == (points["p-one"].id, points["p-two"].id)

        link = db.scalar(select(QuestionPoint))
        assert link.question_id == "q-one" and link.point_id == points["p-one"].id
        assert db.scalar(select(QuestionConcept)) is not None
        assert db.scalar(select(ConceptEdge)) is not None


def test_points_wait_for_their_material(tmp_path, remote) -> None:  # noqa: ANN001
    """材料还没拉下来（library_cloud 没跑）时，点是**跳过**，不是报错。

    手机上首次同步就是这种时序：题库包先到、资料包后到 —— 那一轮只能把题目装进去，
    知识点等下一轮补齐。所以这里要的是"跳过并记账"，不是"整包失败"。
    """
    machine_a = _machine(tmp_path, "a")
    machine_b = _machine(tmp_path, "b")
    _seed(machine_a)

    with _session(machine_a) as db:
        bank_cloud.push(db, remote=remote)
    with _session(machine_b) as db:
        result = bank_cloud.pull(db, remote=remote)

    assert result["skippedPoints"] == 2
    assert result["questions"] == 1
    with _session(machine_b) as db:
        assert db.scalar(select(Question).where(Question.id == "q-one")) is not None
        assert db.scalar(select(KnowledgePoint)) is None


def test_empty_bank_never_overwrites_the_cloud(tmp_path, remote) -> None:  # noqa: ANN001
    """本机没有题 → 不推。云端可能是另一台机器整理好的题库。"""
    machine = _machine(tmp_path, "empty")
    _seed(machine)

    # 先走一轮真同步（建立标尺），再清空本地题库 —— 这就是"换了一台空设备"的样子
    with _session(machine) as db:
        assert bank_cloud.sync_pack(db, remote=remote)["action"] == "push"

    with _session(machine) as db:
        for table in ("question_points", "question_concepts", "questions", "concept_edges",
                      "point_edges", "point_sources", "knowledge_points", "concepts",
                      "topics", "meta_documents"):
            db.execute(text(f"DELETE FROM {table}"))
        db.commit()
        result = bank_cloud.sync_pack(db, remote=remote)

    assert result["action"] == "idle"
    assert "不覆盖云端" in result["reason"]
    blocks = remote.fake.rows("SELECT payload FROM qf_bank_block")
    assert "q-one" in blocks[0]["payload"], "云端那份题库必须原样还在"


def test_sync_pack_settles_to_idle_after_the_first_push(tmp_path, remote) -> None:  # noqa: ANN001
    """推一次之后每轮都该是 idle —— 否则后台节拍会一直重传 13MB。"""
    machine = _machine(tmp_path, "steady")
    _seed(machine)

    with _session(machine) as db:
        first = bank_cloud.sync_pack(db, remote=remote)
    assert first["action"] == "push"

    with _session(machine) as db:
        second = bank_cloud.sync_pack(db, remote=remote)
    assert second["action"] == "idle"

    # 改一道题（行数不变、内容变了）→ 指纹要能发现
    with _session(machine) as db:
        question = db.scalar(select(Question).where(Question.id == "q-one"))
        question.content_hash = "h-two"
        db.commit()
        third = bank_cloud.sync_pack(db, remote=remote)
    assert third["action"] == "push"


@pytest.fixture()
def remote():
    """假 Turso（复用 `test_sync.py` 那台），包成真的 `TursoClient`。

    `fake` 挂在 client 上，方便断言"云端到底存了什么"。
    """
    from test_sync import FakeTurso

    fake = FakeTurso()
    client = sync.TursoClient(fake.url, "test-token")
    client.fake = fake  # type: ignore[attr-defined]
    yield client
    fake.close()
