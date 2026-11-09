"""从**切片正文**读材料（原文件不在本机时的唯一来源）。

`material_slices.text` 是从原文件按 `[start_line, end_line]` 截出来的那一段，
所以它带着原文件的行号：切片的第 i 行 = `start_line + i`。

换一台机器、拉过 `make library-pull` 之后：材料行（`materials`）在、切片在、
正文也在，于是 `read_material` 照样能按行区间读 —— 这就是"换台机器也能访问材料库"。

**懒加载**（`lazy_bodies`，手机端）：同步只落条目与坐标，`text` 是空的。
这时按片去云端取（`app/slice_cloud.py`），命中过的块留在本机缓存；
取不到就说取不到，不拿别的行凑。

边界：切片没覆盖到的行（材料末尾漏几行、或某片没截正文）会缺，缺了就说缺。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from .models import Material, MaterialSlice


def _body(db: Any, material: Material, row: MaterialSlice) -> str:
    """这一片的正文：本地有就用本地，没有就（懒加载时）去云端取。"""
    if row.text:
        return row.text
    from .config import get_settings  # noqa: PLC0415 - 避免循环导入

    if not get_settings().lazy_bodies:
        return ""
    from . import slice_cloud  # noqa: PLC0415

    return slice_cloud.fetch(db, material.slug, row.slice_id)


def has_text(db: Any, material: Material) -> bool:
    """这份材料能不能读到正文。

    懒加载时看"有没有切片"——正文在云端，取不取得到要等真正打开才知道，
    不能因为本机没存就报"没有正文"。
    """
    from .config import get_settings  # noqa: PLC0415

    query = select(MaterialSlice.id).where(MaterialSlice.material_id == material.id)
    if not get_settings().lazy_bodies:
        query = query.where(MaterialSlice.text != "")
    return db.scalar(query.limit(1)) is not None


def read_lines(db: Any, material: Material, start: int = 1, end: int = 0, *, max_lines: int = 400) -> list[dict]:
    """按行区间从切片正文里取（1-based，含两端）。返回形状与 `materials.read_lines` 一致。"""
    start = max(1, int(start or 1))
    end = int(end or 0) or start + 59
    end = min(end, start + max_lines - 1)

    rows = db.scalars(
        select(MaterialSlice)
        .where(
            MaterialSlice.material_id == material.id,
            MaterialSlice.end_line >= start,
            MaterialSlice.start_line <= end,
        )
        .order_by(MaterialSlice.start_line)
    ).all()

    out: list[dict] = []
    for row in rows:
        text = _body(db, material, row)
        if not text:
            continue
        lines = text.split("\n")
        for offset, line in enumerate(lines):
            number = int(row.start_line) + offset
            if number < start or number > end:
                continue
            out.append({"line": number, "text": line})
    return out
