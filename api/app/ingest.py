"""导入流水线：一个文件 → 材料 + 切片（**带正文**）。

用户的一句话是这个模块的全部需求（2026-11-08）：

> 用户把文件拖进去，就直接流水线切片向量处理掉了 —— 这就是"正文入库"啊！

所以上传接口落盘之后调这里，一次做完：

   解析成行文本 → 按标题/长度切分 → 写 `materials` + `material_slices`（含 text）
   → （模型就绪时）算向量

切分规则与出题机的 `pipeline/ingest.py` 同源（按 `##`/`###` 标题、太碎的并、太长的拆），
这里复刻一份是因为 `api/` 打包时不该依赖出题机那套重依赖。

边界：原文件**留在本机**（不进包、不上云）；上云的是切片与它的正文（`make library-push`）。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select

from . import materials as mat_mod
from .models import Material, MaterialSlice

#: 标题行：`#`~`####` + 文字（与 `pipeline/ingest.py` 的 HEADING_RE 一致）
HEADING_RE = re.compile(r"^(#{1,4})\s+(.+?)\s*#*\s*$")

MIN_LINES = 12      # 比这还短的片并进下一片
MAX_LINES = 260     # 比这还长的片按行拆开
SPLIT_AT = 180      # 拆的时候每片多少行


def split_lines(lines: list[str]) -> list[tuple[int, int, str]]:
    """按标题切；返回 `[(起行, 止行, 标题), ...]`（1-based，闭区间）。"""
    marks: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        match = HEADING_RE.match(line)
        if match:
            marks.append((index, match.group(2)))
    if not marks:
        marks = [(0, "(全文)")]
    if marks[0][0] != 0:
        marks.insert(0, (0, "(前言)"))
    marks.append((len(lines), ""))

    chunks: list[tuple[int, int, str]] = []
    for (start, title), (next_start, _) in zip(marks, marks[1:]):
        end = max(start + 1, next_start)
        chunks.append((start, min(end, len(lines)), title))

    merged: list[tuple[int, int, str]] = []
    for chunk in chunks:
        if merged and (chunk[1] - chunk[0]) < MIN_LINES:
            prev = merged[-1]
            merged[-1] = (prev[0], chunk[1], prev[2])
        else:
            merged.append(chunk)

    out: list[tuple[int, int, str]] = []
    for start, end, title in merged:
        while end - start > MAX_LINES:
            out.append((start, start + SPLIT_AT, f"{title}（上）"))
            start += SPLIT_AT
        out.append((start, end, title))
    return [(s + 1, e, t) for s, e, t in out if e > s]


def _slugify(text: str, limit: int = 60) -> str:
    out = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "-", str(text or "").lower()).strip("-")
    return out[:limit] or "doc"


def ingest_file(
    db: Any,
    path: Path | str,
    *,
    subject: str = "",
    depth: str = "检索",
    embed: bool = True,
    on_stage: Any = None,
) -> dict:
    """一个文件 → 材料 + 切片（带正文）+（可选）向量。幂等：同一份文件重跑会覆盖切片。

    `on_stage(stage)` 在阶段边界回调（`parse` / `slice` / `embed`）——
    流水线页面靠它显示"这份走到哪一步了"，也是暂停/取消的检查点。

    没有单独的"正文"阶段：切片那一步就已经把正文截好写进 `MaterialSlice.text`
    （见下面那句"切片就是正文"）。以前在这里打过一次空标记，页面上就多出一个
    什么也不做的步骤。
    """

    def stage(name: str) -> None:
        if on_stage is not None:
            on_stage(name)

    target = Path(path)
    stage("parse")
    lines = list(mat_mod._lines(str(target)))
    body = "\n".join(lines)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    slug = _slugify(target.stem) + "-" + digest[:8]

    material = db.scalar(select(Material).where(Material.slug == slug))
    if material is None:
        # 同名的旧材料（内容变了 → 哈希变了 → slug 变了）先放过，保留历史
        material = Material(slug=slug)
        db.add(material)
    material.subject = subject or material.subject or "资料"
    material.title = material.title or target.stem
    material.source_path = str(target)
    material.sha256 = digest
    material.lines = len(lines)
    material.depth = depth
    db.flush()

    # 切片整批换掉（重跑幂等）
    stage("slice")
    db.execute(delete(MaterialSlice).where(MaterialSlice.material_id == material.id))
    pieces = split_lines(lines)
    slices: list[MaterialSlice] = []
    for index, (start, end, title) in enumerate(pieces, start=1):
        slices.append(
            MaterialSlice(
                material_id=material.id,
                slice_id=f"sl-{index:03d}",
                path=[title] if title else [],
                start_line=start,
                end_line=end,
                summary=title,
                # **切片就是正文**：这里直接截出来存好，换台机器靠它读材料
                text="\n".join(lines[start - 1:end]),
                windows=1,
            )
        )
    db.add_all(slices)
    db.commit()

    embedded = 0
    if embed:
        stage("embed")
        try:
            embedded = _embed_slices(db, slices)
        except Exception:  # noqa: BLE001 - 向量是可选的，失败不该让入库失败
            embedded = 0

    return {
        "slug": slug,
        "title": material.title,
        "lines": len(lines),
        "slices": len(slices),
        "chars": sum(len(s.text) for s in slices),
        "embedded": embedded,
    }


def _embed_slices(db: Any, slices: list[MaterialSlice]) -> int:
    """给切片算向量（本地小模型没就绪就直接跳过，留给 `make embed`）。"""
    from . import local_embed  # noqa: PLC0415 - 重依赖，用到才导入

    if not local_embed.status().get("ready"):
        return 0

    texts = [s.text[:6000] for s in slices]
    vectors = local_embed.embed(texts, kind="passage")
    from . import semantic  # noqa: PLC0415
    from .models import SliceEmbedding  # noqa: PLC0415

    count = 0
    try:
        for row, vector in zip(slices, vectors):
            db.add(
                SliceEmbedding(
                    slice_id=row.id,
                    ordinal=1,
                    start_line=row.start_line,
                    end_line=row.end_line,
                    model=str(local_embed.manifest().get("model") or ""),
                    dim=len(vector),
                    # `vec` 是 LargeBinary：float32 紧凑打包（与 `semantic.pack` 同源），
                    # 直接塞 list 会报 "memoryview: a bytes-like object is required"
                    vec=semantic.pack(vector),
                    text_hash=hashlib.sha1(row.text.encode("utf-8")).hexdigest()[:32],
                )
            )
            count += 1
        db.commit()
    except Exception:  # noqa: BLE001 - 向量失败不能连累切片（切片已经提交过了）
        db.rollback()
        raise
    return count
