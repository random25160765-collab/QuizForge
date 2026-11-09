"""按需从云端取切片正文（开了 `lazy_bodies` 的机器，手机端）。

`library_cloud` 把正文按 768 KB 一块写进云端 `qf_library_text`，块里是
`[{material, slice_id, text}, …]`。桌面端整包拉下来回填 `material_slices.text`；
手机端只同步条目与切片坐标，所以这里负责「点开哪片取哪块」：

1. 本机 `material_slices.text` 有 → 调用方直接用，这里不管；
2. 没有 → 先查本机缓存（内存索引 → `library_text_cache` 表）；
3. 还没有 → 从云端**逐块**取（命中即停），取到就落缓存并建索引；
4. 缓存超预算按最久未用淘汰。

**它不是第二套数据源**：缓存是正文的复印件，删掉只会让下次再取一次。
"""

from __future__ import annotations

import json
import time
from typing import Any

from sqlalchemy import func, select

from .models import LibraryTextCache

#: 缓存预算。一块 768 KB，64 MB 够放八十多块（几十份资料的切片）；超了按 `at` 淘汰。
BUDGET_BYTES = 64 * 1024 * 1024

#: "slug|slice_id" -> 块号。进程内索引，免得每次都把缓存块 JSON 解一遍。
_index: dict[str, int] = {}


def _key(slug: str, slice_id: str) -> str:
    return str(slug) + "|" + str(slice_id)


def _find(payload: str, slug: str, slice_id: str) -> str | None:
    """块里找这一片；**找到空串也算找到**（那片本来就没抽到正文）。"""
    try:
        items = json.loads(payload or "[]")
    except ValueError:
        return None
    if not isinstance(items, list):
        return None
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("material") or "") == slug and str(item.get("slice_id") or "") == slice_id:
            return str(item.get("text") or "")
    return None


def _forget(idx: int) -> None:
    for key, value in list(_index.items()):
        if value == idx:
            _index.pop(key, None)


def _trim(db: Any) -> None:
    total = int(db.scalar(select(func.coalesce(func.sum(LibraryTextCache.bytes), 0))) or 0)
    if total <= BUDGET_BYTES:
        return
    for row in db.scalars(select(LibraryTextCache).order_by(LibraryTextCache.at)).all():
        _forget(int(row.idx))
        db.delete(row)
        total -= int(row.bytes or 0)
        if total <= BUDGET_BYTES:
            break
    db.commit()


def _remember(db: Any, idx: int, payload: str) -> None:
    row = db.get(LibraryTextCache, idx)
    if row is None:
        row = LibraryTextCache(idx=idx)
        db.add(row)
    row.payload = payload
    row.bytes = len(payload.encode("utf-8"))
    row.at = time.time()
    db.commit()
    # 顺手把这一块里的每一片都记进索引：同一份材料的切片通常挤在一块，
    # 下一次点开同块里的别的片就不用再解析一遍。
    try:
        for item in json.loads(payload or "[]"):
            if isinstance(item, dict):
                _index[_key(item.get("material") or "", item.get("slice_id") or "")] = idx
    except ValueError:
        pass
    _trim(db)


def fetch(db: Any, slug: str, slice_id: str) -> str:
    """这一片的正文。取不到（没配云 / 网络断 / 云端没这片）返回空串。"""
    key = _key(slug, slice_id)

    idx = _index.get(key)
    if idx is not None:
        row = db.get(LibraryTextCache, idx)
        text = _find(row.payload, slug, slice_id) if row is not None else None
        if text is not None:
            return text
        _index.pop(key, None)

    # 已经缓存过的块里找：同一份材料的切片通常挤在相邻几块
    for row in db.scalars(select(LibraryTextCache).order_by(LibraryTextCache.idx)).all():
        text = _find(row.payload, slug, slice_id)
        if text is not None:
            _index[key] = int(row.idx)
            return text

    try:
        from .library_cloud import _client  # noqa: PLC0415 —— 避免循环导入

        client = _client()
        total = int((client.query("SELECT COUNT(*) AS n FROM qf_library_text") or [{}])[0].get("n") or 0)
        for offset in range(total):
            rows = client.query(
                "SELECT idx, payload FROM qf_library_text ORDER BY idx LIMIT 1 OFFSET ?", (offset,)
            )
            if not rows:
                break
            item = rows[0]
            idx = int(item.get("idx") or 0)
            payload = str(item.get("payload") or "")
            text = _find(payload, slug, slice_id)
            if text is None:
                continue
            _remember(db, idx, payload)
            _index[key] = idx
            return text
    except Exception:  # noqa: BLE001 - 没配云 / 表还没建：当作取不到，别把资料页打崩
        return ""
    return ""


def slim(db: Any) -> dict:
    """懒加载的机器启动时瘦一次身：把已经整包落下来的正文与向量清掉。

    老版本（2026-11-09 之前）在手机上也会整包落地：`material_slices.text`
    约 39MB、`slice_embeddings` 约 168MB。升级后这些不会再写，但**旧的还在**，
    所以启动时清一次；正文随时能从云端按片取回（`fetch`）。

    只在 `lazy_bodies` 时动手；清完 `VACUUM` 一次，文件才真的变小
    （SQLite 删行只把页标成空闲）。
    """
    from sqlalchemy import delete, update  # noqa: PLC0415

    from .config import get_settings  # noqa: PLC0415
    from .models import MaterialSlice, SliceEmbedding  # noqa: PLC0415

    if not get_settings().lazy_bodies:
        return {"skipped": True}

    text_rows = int(
        db.execute(update(MaterialSlice).where(MaterialSlice.text != "").values(text="")).rowcount or 0
    )
    embed_rows = int(db.execute(delete(SliceEmbedding)).rowcount or 0)
    db.commit()

    if text_rows or embed_rows:
        # VACUUM 不能跑在事务里：单开一个 AUTOCOMMIT 连接
        engine = db.get_bind()
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.exec_driver_sql("VACUUM")
    return {"textSlices": text_rows, "embeddings": embed_rows}


__all__ = ["BUDGET_BYTES", "fetch", "slim"]
