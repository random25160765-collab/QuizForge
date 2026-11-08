"""资料切片整包上传/下载（材料 + 切片**正文** + 图）。

## 为什么是"整包"而不是行级同步

现有 sync 是**行级**的：每张表按本地主键 `id` 生成 uid 再逐行推。资料这三张表
（`materials` / `material_slices` / `material_figures`）是出题流水线写的，
主键是自增 id、切片还带 `material_id` 外键 —— 直接纳入行级同步，两台机器上
"同一个材料不同 id"会打架（`conversation_folders` 那次就撞过唯一约束）。

而资料的变更不频繁（拖进来、重新拆分时才变），**整包替换**最省事：
一个元数据包 + 若干正文分块，拉回来按业务键（slug / slice_id / figure_id）upsert。

## 正文为什么分块

切片正文合计 **31.7 MB**（3989 片全覆盖）。Turso 的单次 pipeline 请求实测
超过几 MB 就被掐断（`Connection reset by peer`），所以正文按 768 KB 一块写进
`qf_library_text`，元数据包里记块数与总字节以便核对。

## 边界（用户 2026-11-08 定的）

* **切片就是正文**：`material_slices.text` 是从原文件按行区间截出来的那一段，
  换一台机器靠它读材料（原文件本身不上云）；
* 资料**不加密**（与对话那套端到端加密不同）。
"""

from __future__ import annotations

import json
import time
from typing import Any

from sqlalchemy import select

from . import sync
from .models import Material, MaterialFigure, MaterialSlice

PACK_ID = 1

#: 正文一块多少字节。768 KB 在实测里很稳（6.9 MB 那次会断）。
TEXT_BLOCK_BYTES = 768 * 1024

#: 云端那两张表。放在 `qf_sync_*` 旁边，但不进那套行级机制（互不干扰）。
REMOTE_TABLE = """
CREATE TABLE IF NOT EXISTS qf_library (
  id      INTEGER PRIMARY KEY,
  at      REAL NOT NULL,
  origin  TEXT NOT NULL,
  bytes   INTEGER NOT NULL DEFAULT 0,
  payload TEXT
)
"""

REMOTE_TEXT_TABLE = """
CREATE TABLE IF NOT EXISTS qf_library_text (
  idx     INTEGER PRIMARY KEY,
  bytes   INTEGER NOT NULL DEFAULT 0,
  payload TEXT
)
"""


def _client() -> Any:
    cfg = sync.turso_config()
    if not cfg.get("url") or not cfg.get("token"):
        raise RuntimeError(
            "还没配云同步：先在设置里填 Turso 地址与令牌（或写 `config/settings.local.json` 的 turso 块）"
        )
    return sync.TursoClient(cfg["url"], cfg["token"])


def _origin() -> str:
    from .db import get_engine  # noqa: PLC0415 - 避免循环导入

    return sync._origin(get_engine())  # noqa: SLF001


def pack(db: Any, *, with_text: bool = False) -> dict:
    """把本地三张表打成一份可传输的 JSON（用业务键，不用自增 id）。

    `with_text=False` 时**不带正文**（正文走 `text_blocks()` 分块传）。
    """
    materials = db.scalars(select(Material)).all()
    slug_of = {m.id: m.slug for m in materials}

    out_materials = [
        {
            "slug": m.slug,
            "subject": m.subject,
            "title": m.title,
            "lines": m.lines,
            "sha256": m.sha256,
            "depth": m.depth,
        }
        for m in materials
    ]
    out_slices = []
    for s in db.scalars(select(MaterialSlice)).all():
        slug = slug_of.get(s.material_id)
        if not slug:
            continue
        item = {
            "material": slug,
            "slice_id": s.slice_id,
            "path": s.path,
            "start_line": s.start_line,
            "end_line": s.end_line,
            "tokens": s.tokens,
            "figures": s.figures,
            "summary": s.summary,
            "windows": s.windows,
        }
        if with_text:
            item["text"] = s.text
        out_slices.append(item)

    out_figures = []
    for f in db.scalars(select(MaterialFigure)).all():
        slug = slug_of.get(f.material_id)
        if not slug:
            continue
        out_figures.append(
            {
                "material": slug,
                "figure_id": f.figure_id,
                "src": f.src,
                "slice_id": f.slice_id,
                "start_line": f.start_line,
                "end_line": f.end_line,
                "caption": f.caption,
                "kind": f.kind,
                "keys": f.keys,
                "plan": f.plan,
            }
        )
    return {
        "version": 2,
        "materials": out_materials,
        "slices": out_slices,
        "figures": out_figures,
    }


def text_blocks(db: Any, *, limit: int = TEXT_BLOCK_BYTES) -> list[tuple[int, str, int]]:
    """切片正文 → 若干块 `(idx, json, bytes)`，每块不超过 `limit`。"""
    materials = db.scalars(select(Material)).all()
    slug_of = {m.id: m.slug for m in materials}

    blocks: list[tuple[int, str, int]] = []
    buf: list[dict] = []
    size = 0
    for s in db.scalars(select(MaterialSlice).order_by(MaterialSlice.id)).all():
        slug = slug_of.get(s.material_id)
        if not slug or not s.text:
            continue
        item = {"material": slug, "slice_id": s.slice_id, "text": s.text}
        chunk = len(json.dumps(item, ensure_ascii=False).encode("utf-8"))
        if buf and size + chunk > limit:
            blob = json.dumps(buf, ensure_ascii=False)
            blocks.append((len(blocks), blob, len(blob.encode("utf-8"))))
            buf, size = [], 0
        buf.append(item)
        size += chunk
    if buf:
        blob = json.dumps(buf, ensure_ascii=False)
        blocks.append((len(blocks), blob, len(blob.encode("utf-8"))))
    return blocks


def push(db: Any, *, remote: Any | None = None) -> dict:
    """把整包（元数据 + 正文分块）写到云端，覆盖上一次。"""
    data = pack(db)
    blocks = text_blocks(db)
    data["textBlocks"] = len(blocks)
    data["textBytes"] = sum(size for _, _, size in blocks)

    blob = json.dumps(data, ensure_ascii=False)
    client = remote or _client()
    client.execute(REMOTE_TABLE)
    client.execute(REMOTE_TEXT_TABLE)
    client.execute(
        "INSERT OR REPLACE INTO qf_library (id, at, origin, bytes, payload) VALUES (?, ?, ?, ?, ?)",
        (PACK_ID, time.time(), _origin(), len(blob.encode("utf-8")), blob),
    )
    client.execute("DELETE FROM qf_library_text")
    for idx, payload, size in blocks:
        client.execute(
            "INSERT INTO qf_library_text (idx, bytes, payload) VALUES (?, ?, ?)",
            (idx, size, payload),
        )
    return {
        "materials": len(data["materials"]),
        "slices": len(data["slices"]),
        "figures": len(data["figures"]),
        "bytes": len(blob.encode("utf-8")),
        "textBlocks": len(blocks),
        "textBytes": data["textBytes"],
    }


def pull(db: Any, *, remote: Any | None = None) -> dict:
    """从云端拉整包，按业务键 upsert 到本地三张表（含切片正文）。"""
    client = remote or _client()
    try:
        rows = client.query("SELECT payload, bytes FROM qf_library WHERE id = ?", (PACK_ID,))
    except Exception:  # noqa: BLE001 - 表还没建过
        return {"materials": 0, "slices": 0, "figures": 0, "bytes": 0, "empty": True}
    if not rows:
        return {"materials": 0, "slices": 0, "figures": 0, "bytes": 0, "empty": True}

    data = json.loads(rows[0]["payload"] or "{}")
    by_slug = {m.slug: m for m in db.scalars(select(Material)).all()}

    added = updated = 0
    for item in data.get("materials", []):
        slug = str(item.get("slug") or "")
        if not slug:
            continue
        hit = by_slug.get(slug)
        if hit is None:
            hit = Material(
                slug=slug,
                subject=str(item.get("subject") or ""),
                title=str(item.get("title") or ""),
                # 原文件不在这台机器上：路径留空，正文走切片（`material_slices.text`）
                source_path="",
                sha256=str(item.get("sha256") or ""),
                lines=int(item.get("lines") or 0),
                depth=str(item.get("depth") or "检索"),
            )
            db.add(hit)
            by_slug[slug] = hit
            added += 1
        else:
            hit.subject = str(item.get("subject") or hit.subject)
            hit.title = str(item.get("title") or hit.title)
            hit.lines = int(item.get("lines") or hit.lines)
            hit.sha256 = str(item.get("sha256") or hit.sha256)
            hit.depth = str(item.get("depth") or hit.depth)
            updated += 1
    db.flush()

    # 切片与图：按 (material_id, slice_id/figure_id) 覆盖
    slices = {(s.material_id, s.slice_id): s for s in db.scalars(select(MaterialSlice)).all()}
    for item in data.get("slices", []):
        material = by_slug.get(str(item.get("material") or ""))
        if material is None:
            continue
        key = (material.id, str(item.get("slice_id") or ""))
        hit = slices.get(key)
        if hit is None:
            hit = MaterialSlice(material_id=material.id, slice_id=key[1])
            db.add(hit)
            slices[key] = hit
        hit.path = item.get("path") or []
        hit.start_line = int(item.get("start_line") or 0)
        hit.end_line = int(item.get("end_line") or 0)
        hit.tokens = str(item.get("tokens") or "")
        hit.figures = item.get("figures") or []
        hit.summary = str(item.get("summary") or "")
        hit.windows = int(item.get("windows") or 0)

    figures = {(f.material_id, f.figure_id): f for f in db.scalars(select(MaterialFigure)).all()}
    for item in data.get("figures", []):
        material = by_slug.get(str(item.get("material") or ""))
        if material is None:
            continue
        key = (material.id, str(item.get("figure_id") or ""))
        hit = figures.get(key)
        if hit is None:
            hit = MaterialFigure(material_id=material.id, figure_id=key[1])
            db.add(hit)
            figures[key] = hit
        hit.src = str(item.get("src") or "")
        hit.slice_id = str(item.get("slice_id") or "")
        hit.start_line = int(item.get("start_line") or 0)
        hit.end_line = int(item.get("end_line") or 0)
        hit.caption = str(item.get("caption") or "")
        hit.kind = str(item.get("kind") or "")
        hit.keys = item.get("keys") or []
        # `plan` 是 Text 列（一句话说明），传 dict 会报 "type 'dict' is not supported"
        hit.plan = str(item.get("plan") or "")

    db.flush()

    # 正文：从分块里按 (slug, slice_id) 回填
    text_slices = filled = 0
    try:
        blocks = client.query("SELECT payload FROM qf_library_text ORDER BY idx")
    except Exception:  # noqa: BLE001 - 老包里没有这张表
        blocks = []
    for row in blocks:
        try:
            items = json.loads(row.get("payload") or "[]")
        except Exception:  # noqa: BLE001
            continue
        for item in items:
            material = by_slug.get(str(item.get("material") or ""))
            if material is None:
                continue
            hit = slices.get((material.id, str(item.get("slice_id") or "")))
            if hit is None:
                continue
            hit.text = str(item.get("text") or "")
            text_slices += 1
            filled += len(hit.text)

    db.commit()
    return {
        "materials": added + updated,
        "added": added,
        "updated": updated,
        "slices": len(data.get("slices", [])),
        "figures": len(data.get("figures", [])),
        "bytes": int(rows[0].get("bytes") or 0),
        "textBlocks": len(blocks),
        "textSlices": text_slices,
        "textBytes": filled,
    }
