"""向量阶段的批大小与落盘粒度。

来由：2026-11-13 那份 87MB 的 PDF（19740 片）第一次跑到"向量"，整份材料被
一次性塞给模型，ONNX 要 41GB 张量，`Failed to allocate memory`；而 `ingest`
把向量化整段包在 `except: embedded = 0` 里 —— 任务显示"完成 · 0 条向量"，
检索里这份材料等于不存在。这里钉住两件事：**一批不超过 EMBED_BATCH**、
**已经算完的批次不许跟着后面那批一起丢**。
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, select

from app import ingest, local_embed
from app.db import get_session_factory
from app.models import Material, MaterialSlice, SliceEmbedding


def _seed(count: int) -> int:
    session = get_session_factory()()
    try:
        material = Material(slug=f"test-embed-{uuid.uuid4().hex[:8]}", subject="测试")
        session.add(material)
        session.flush()
        session.add_all(
            [
                MaterialSlice(
                    material_id=material.id,
                    slice_id=f"sl-{index:03d}",
                    path=[],
                    start_line=index,
                    end_line=index,
                    text=f"第 {index} 片的正文",
                )
                for index in range(1, count + 1)
            ]
        )
        session.commit()
        return int(material.id)
    finally:
        session.close()


def _drop(material_id: int) -> None:
    """材料与切片是 bank/derived 表，不在逐用例清理名单里，只能自己收尾。"""
    session = get_session_factory()()
    try:
        slice_ids = select(MaterialSlice.id).where(MaterialSlice.material_id == material_id)
        session.execute(delete(SliceEmbedding).where(SliceEmbedding.slice_id.in_(slice_ids)))
        session.execute(delete(MaterialSlice).where(MaterialSlice.material_id == material_id))
        session.execute(delete(Material).where(Material.id == material_id))
        session.commit()
    finally:
        session.close()


def _fake_embed(monkeypatch, *, fail_at: int | None = None):  # noqa: ANN001, ANN202
    batches: list[int] = []

    def embed(texts, *, kind="passage"):  # noqa: ANN001, ANN202
        batches.append(len(texts))
        if fail_at is not None and len(batches) == fail_at:
            raise RuntimeError("Failed to allocate memory")
        return [[float(len(text))] * 4 for text in texts]

    monkeypatch.setattr(local_embed, "status", lambda: {"ready": True})
    monkeypatch.setattr(local_embed, "manifest", lambda: {"model": "fake-model"})
    monkeypatch.setattr(local_embed, "embed", embed)
    return batches


def _rows(material_id: int) -> int:
    session = get_session_factory()()
    try:
        slice_ids = select(MaterialSlice.id).where(MaterialSlice.material_id == material_id)
        return len(session.scalars(select(SliceEmbedding.id).where(SliceEmbedding.slice_id.in_(slice_ids))).all())
    finally:
        session.close()


def test_slices_are_sent_to_the_model_in_batches(client, monkeypatch) -> None:  # noqa: ANN001
    """整份材料不能一次送模型 —— 批大小由内存定，不由切片数定。"""
    material_id = _seed(20)
    try:
        session = get_session_factory()()
        try:
            slices = session.scalars(
                select(MaterialSlice)
                .where(MaterialSlice.material_id == material_id)
                .order_by(MaterialSlice.id)
            ).all()
            seen: list[tuple[int, int]] = []
            batches = _fake_embed(monkeypatch)
            count = ingest._embed_slices(
                session, list(slices), on_progress=lambda done, total: seen.append((done, total))
            )  # 这一段是 `_embed_slices` 自己的口径，标签由 `ingest_file` 加
        finally:
            session.close()

        assert batches == [
            min(ingest.EMBED_BATCH, 20 - offset) for offset in range(0, 20, ingest.EMBED_BATCH)
        ]
        assert count == 20
        assert _rows(material_id) == 20
        assert seen[-1] == (20, 20), "进度要报总数，页面才知道还剩多少"
    finally:
        _drop(material_id)


def test_a_broken_batch_keeps_what_was_already_committed(client, monkeypatch) -> None:  # noqa: ANN001
    """第二批炸了，第一批的向量要留在库里 —— 长活重来一次的代价是几十分钟。"""
    material_id = _seed(20)
    try:
        session = get_session_factory()()
        try:
            slices = list(
                session.scalars(
                    select(MaterialSlice)
                    .where(MaterialSlice.material_id == material_id)
                    .order_by(MaterialSlice.id)
                ).all()
            )
            _fake_embed(monkeypatch, fail_at=2)
            count = ingest._embed_slices(session, slices)
        finally:
            session.close()

        assert count == ingest.EMBED_BATCH
        assert _rows(material_id) == ingest.EMBED_BATCH
    finally:
        _drop(material_id)
