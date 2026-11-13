"""材料的**行文本**：解析、分块入库、按行区间读。

## 为什么需要这一层

资料原先只有「原文件 + 切片指针」：`materials.source_path` 指向本机的 PDF/MD，
`material_slices` 只记行区间，正文在文件里。于是 chat 的 `read_material` 只能在
**有那个文件的那台机器**上工作。

用户定的新形态（2026-11-08）：

* 云端存**拆分完的材料正文**（纯文本），原文件不上云；
* 本地把文件拖进来就算"入库"，解析与切分都在本地做；
* chat 直接读云端那份正文 —— 换一台机器、本地没有原文件也能读。

## 边界

* **不加密**（用户："资料不用加密"）—— 与对话那一套端到端加密不同，
  所以这两张表不进 `sync.PRIVATE_TABLES`。
* **原文件不传**：这里只写"规范化后的行文本"，与 `materials.read_lines`
  返回的行**逐字一致**，引用点回原文才对得上。
* 分块（64KB）是为了传输：整份材料最大 17.8 万行，一行 payload 会被 Turso 掐断。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select

from .models import LibraryChunk, LibraryDoc

#: 一块多少字。64KB 在 Turso 的 pipeline 请求里很安全（那边单请求超过几 MB 会被断）。
CHUNK_CHARS = 64 * 1024


def _slugify(text: str, limit: int = 60) -> str:
    out = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "-", str(text or "").lower()).strip("-")
    return out[:limit] or "doc"


def slug_for(path: Path, digest: str) -> str:
    """材料 slug：文件名 slug 化 + 内容哈希前 8 位。

    带哈希是为了"同名不同内容"不打架（同一份文件再拖一次则 slug 相同，
    上传变成覆盖 —— 这正是去重想要的）。
    """
    return _slugify(path.stem) + "-" + digest[:8]


def _lines_of(path: Path) -> list[str]:
    """本地文件 → 行文本。走 `materials.text_lines`（它认得 pdf/docx 那几种）。

    这里原先直接调 `_lines`（纯按 UTF-8 读），PDF 进来就是一屏 `%PDF` 与压缩流 ——
    与流水线那边是同一个坑，见 `materials.text_lines` 的注释。
    """
    from . import materials  # noqa: PLC0415 - 避免顶层循环导入

    return materials.text_lines(str(path))[0]


def ingest(db: Any, path: Path, *, slug: str = "", title: str = "") -> dict:
    """把一份本地文件解析成行文本、分块写库。返回一份摘要。

    同一份文件（sha256 相同、slug 相同）再跑一次是幂等的：老分块整批换掉。
    """
    lines = _lines_of(path)
    raw = "\n".join(lines).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    doc_slug = slug or slug_for(path, digest)

    doc = db.scalar(select(LibraryDoc).where(LibraryDoc.slug == doc_slug))
    if doc is None:
        doc = LibraryDoc(slug=doc_slug)
        db.add(doc)
    doc.title = title or path.stem
    doc.sha256 = digest
    doc.lines = len(lines)
    doc.bytes = len(raw)
    db.flush()

    # 重写分块：先清旧的，再按行装箱（不切裂一行）
    db.execute(delete(LibraryChunk).where(LibraryChunk.doc_slug == doc_slug))
    chunks: list[LibraryChunk] = []
    buf: list[str] = []
    size = 0
    start = 1
    for number, line in enumerate(lines, start=1):
        buf.append(line)
        size += len(line) + 1
        if size >= CHUNK_CHARS:
            chunks.append(
                LibraryChunk(
                    doc_slug=doc_slug,
                    idx=len(chunks),
                    start_line=start,
                    lines=len(buf),
                    text="\n".join(buf),
                )
            )
            buf, size, start = [], 0, number + 1
    if buf:
        chunks.append(
            LibraryChunk(
                doc_slug=doc_slug,
                idx=len(chunks),
                start_line=start,
                lines=len(buf),
                text="\n".join(buf),
            )
        )
    db.add_all(chunks)
    doc.chunks = len(chunks)
    db.commit()

    return {
        "slug": doc_slug,
        "title": doc.title,
        "lines": doc.lines,
        "bytes": doc.bytes,
        "chunks": doc.chunks,
        "sha256": digest,
    }


def has_doc(db: Any, slug: str) -> bool:
    return db.scalar(select(LibraryDoc.id).where(LibraryDoc.slug == slug)) is not None


def read_lines(db: Any, slug: str, start: int = 1, end: int = 0, *, max_lines: int = 400) -> list[dict]:
    """按行区间读正文（1-based，含两端）。行号越界就夹到范围内。

    返回形状与 `materials.read_lines` 一致：`[{"line": n, "text": "..."}]` ——
    上层（`tools.read_material`、前端引用抽屉）不用改。
    """
    start = max(1, int(start or 1))
    end = int(end or 0) or start + 59
    end = min(end, start + max_lines - 1)

    rows = db.scalars(
        select(LibraryChunk)
        .where(LibraryChunk.doc_slug == slug, LibraryChunk.start_line <= end)
        .order_by(LibraryChunk.idx)
    ).all()

    out: list[dict] = []
    for chunk in rows:
        chunk_end = chunk.start_line + chunk.lines - 1
        if chunk_end < start:
            continue
        offset = max(0, start - chunk.start_line)
        for i, line in enumerate(chunk.text.split("\n")):
            if i < offset:
                continue
            number = chunk.start_line + i
            if number > end:
                break
            out.append({"line": number, "text": line})
        if out and len(out) >= (end - start + 1):
            break
    return out


def stats(db: Any) -> dict:
    docs = db.scalar(select(LibraryDoc.id).where(LibraryDoc.id > 0)) is not None
    if not docs:
        return {"docs": 0, "chunks": 0, "lines": 0}
    count = len(db.scalars(select(LibraryDoc.id)).all())
    total_lines = sum(db.scalars(select(LibraryDoc.lines)).all())
    chunks = len(db.scalars(select(LibraryChunk.id)).all())
    return {"docs": count, "chunks": chunks, "lines": total_lines}
