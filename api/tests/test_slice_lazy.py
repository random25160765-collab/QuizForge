"""资料正文懒加载（手机端）：条目落地，正文点开才取。

三条不变量：

1. `pull(bodies=False)` 只落条目与切片坐标，`material_slices.text` 保持空；
2. `slice_cloud.fetch` 从云端块里取到正文，第二次走本机缓存（不再连云端）；
3. 懒加载的机器**只读**：云端没包时绝不推一份没有正文的空包上去。
"""

from __future__ import annotations

import json

from app import library_cloud, slice_cloud
from app.config import get_settings
from app.models import LibraryTextCache, Material, MaterialSlice


class FakeRemote:
    """只认资料包那几条 SQL 的假云端（返回形状与 `TursoClient.query` 一致）。"""

    def __init__(self, *, blocks=None, pack=None) -> None:
        self.blocks = blocks or []
        self.pack = pack
        self.reads = 0
        self.writes: list[str] = []

    def execute(self, sql, args=()) -> None:  # noqa: ANN001
        self.writes.append(sql)

    def query(self, sql, args=()) -> list[dict]:  # noqa: ANN001
        if "FROM qf_library WHERE id" in sql:
            return [self.pack] if self.pack else []
        if "COUNT(*) AS n FROM qf_library_text" in sql:
            return [{"n": len(self.blocks)}]
        if "FROM qf_library_text" in sql:
            self.reads += 1
            idx, payload = self.blocks[int(args[0])]
            return [{"idx": idx, "payload": payload}]
        return []


def _pack(at: float = 1.0) -> dict:
    payload = {
        "version": 2,
        "materials": [{"slug": "m1", "title": "材料一", "lines": 10}],
        "slices": [
            {"material": "m1", "slice_id": "s0", "start_line": 1, "end_line": 5},
            {"material": "m1", "slice_id": "s1", "start_line": 6, "end_line": 10},
        ],
        "figures": [],
    }
    return {"at": at, "origin": "test", "bytes": 10, "payload": json.dumps(payload)}


def _material(db, slug: str = "m1") -> Material:  # noqa: ANN001
    material = Material(slug=slug, subject="", title="材料一", source_path="", sha256="", lines=10)
    db.add(material)
    db.commit()
    return material


def test_pull_without_bodies_keeps_coordinates_only(db_session, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(get_settings(), "lazy_bodies", True)
    _material(db_session)
    db_session.add(LibraryTextCache(idx=0, payload="[]", bytes=2, at=0.0))
    db_session.commit()

    out = library_cloud.pull(db_session, remote=FakeRemote(pack=_pack()), bodies=False)

    assert out["textSlices"] == 0
    rows = db_session.query(MaterialSlice).all()
    assert len(rows) == 2
    assert all(row.text == "" for row in rows)
    # 条目变过 → 旧的正文块缓存作废
    assert db_session.query(LibraryTextCache).count() == 0


def test_fetch_pulls_one_block_then_serves_from_cache(db_session, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(get_settings(), "lazy_bodies", True)
    block = json.dumps([{"material": "m1", "slice_id": "s0", "text": "第一行\n第二行"}])
    remote = FakeRemote(blocks=[(0, block)])
    monkeypatch.setattr(library_cloud, "_client", lambda: remote)
    slice_cloud._index.clear()

    assert slice_cloud.fetch(db_session, "m1", "s0") == "第一行\n第二行"
    assert remote.reads == 1

    # 连内存索引都清掉：仍然从表里命中，不再连云端
    slice_cloud._index.clear()
    assert slice_cloud.fetch(db_session, "m1", "s0") == "第一行\n第二行"
    assert remote.reads == 1


def test_lazy_machine_never_pushes_an_empty_pack(db_session, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(get_settings(), "lazy_bodies", True)
    _material(db_session, "m3")
    remote = FakeRemote(pack=None)

    out = library_cloud.sync_pack(db_session, remote=remote)

    assert out["action"] == "idle"
    assert not any("INSERT OR REPLACE INTO qf_library" in sql for sql in remote.writes)


def test_slim_drops_local_bodies(db_session, monkeypatch) -> None:  # noqa: ANN001
    """升级上来的老库：懒加载开启后，整包落的正文要清掉（否则体积不会变小）。"""
    monkeypatch.setattr(get_settings(), "lazy_bodies", True)
    material = _material(db_session, "m-slim")
    db_session.add(
        MaterialSlice(material_id=material.id, slice_id="s0", start_line=1, end_line=2, text="正文")
    )
    db_session.commit()

    info = slice_cloud.slim(db_session)

    assert info["textSlices"] == 1
    db_session.expire_all()
    assert db_session.query(MaterialSlice).filter(MaterialSlice.text != "").count() == 0


def test_endpoint_falls_back_to_slice_text(client, db_session, tmp_path) -> None:  # noqa: ANN001
    """原文件不在本机时，`/knowledge/material` 走切片正文（对话里点引用就是这条路）。"""
    material = Material(
        slug="m-fallback",
        subject="",
        title="材料",
        source_path=str(tmp_path / "gone.md"),
        sha256="",
        lines=3,
    )
    db_session.add(material)
    db_session.commit()
    db_session.add(
        MaterialSlice(
            material_id=material.id,
            slice_id="s0",
            start_line=1,
            end_line=3,
            text="第一行\n第二行\n第三行",
        )
    )
    db_session.commit()

    res = client.get("/api/knowledge/material?slug=m-fallback&start=1&end=3")

    assert res.status_code == 200, res.text
    assert [item["text"] for item in res.json()["lines"]] == ["第一行", "第二行", "第三行"]
