"""从**切片正文**读材料（原文件不在本机时的唯一来源）。

`material_slices.text` 是从原文件按 `[start_line, end_line]` 截出来的那一段，
所以它带着原文件的行号：切片的第 i 行 = `start_line + i`。

换一台机器、拉过 `make library-pull` 之后：材料行（`materials`）在、切片在、
正文也在，于是 `read_material` 照样能按行区间读 —— 这就是"换台机器也能访问材料库"。

边界：切片没覆盖到的行（材料末尾漏几行、或某片没截正文）会缺，缺了就说缺，
不拿别的行凑。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from .models import Material, MaterialSlice


def has_text(db: Any, material: Material) -> bool:
    return db.scalar(
        select(MaterialSlice.id)
        .where(MaterialSlice.material_id == material.id, MaterialSlice.text != "")
        .limit(1)
    ) is not None


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
            MaterialSlice.text != "",
        )
        .order_by(MaterialSlice.start_line)
    ).all()

    out: list[dict] = []
    for row in rows:
        lines = row.text.split("\n")
        for offset, text in enumerate(lines):
            number = int(row.start_line) + offset
            if number < start or number > end:
                continue
            out.append({"line": number, "text": text})
    return out
