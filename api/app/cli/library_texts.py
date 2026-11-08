"""把**切片正文**从原文件截出来，写进 `material_slices.text`。

        make library-texts              # 只补还没填的
        make library-texts FORCE=1      # 全部重截
        make library-texts LIMIT=5      # 先试几份

## 为什么要这一步

切片原先只有「行区间 + 摘要」，正文留在原文件里 —— 换一台机器就读不到材料。
用户要的是"material 就是正文的切片，换台机器也能访问材料库"，所以正文要跟着切片走：
这里按 `[start_line, end_line]` 从原文件把那段截出来存进切片，`make library-push`
再把它整包推到云端。

切片覆盖 625333 行 ≈ 材料总行数 624722（几乎完整覆盖全文），所以这一步之后
云端那份包就等于**全文**（约 40 MB），而不是之前的 1 MB 结构。

慢是真的慢：瓶颈是 `pdftotext` 解析（每份 10–20 秒），188 份约 30–45 分钟。
所以它是个**一次性**命令，跑完就不用再管了。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlalchemy import select

from .. import materials as mat_mod
from ..db import get_session_factory
from ..models import Material, MaterialSlice


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把切片正文从原文件截进库")
    parser.add_argument("--force", action="store_true", help="已经填过的也重截")
    parser.add_argument("--limit", type=int, default=0, help="最多处理几份材料")
    args = parser.parse_args(argv)

    done = skipped = failed = 0
    chars = 0

    with get_session_factory()() as session:
        query = select(Material).order_by(Material.id)
        if args.limit:
            query = query.limit(args.limit)
        mats = session.scalars(query).all()
        print(f"待处理材料：{len(mats)} 份")

        for index, material in enumerate(mats, start=1):
            path = mat_mod.source_file(material)
            if not path or not Path(path).exists():
                skipped += 1
                continue
            slices = session.scalars(
                select(MaterialSlice)
                .where(MaterialSlice.material_id == material.id)
                .order_by(MaterialSlice.start_line)
            ).all()
            if not slices:
                skipped += 1
                continue
            if not args.force and all(s.text for s in slices):
                skipped += 1
                continue

            try:
                lines = mat_mod._lines(path)  # noqa: SLF001 - 与 read_lines 同源
            except Exception as exc:  # noqa: BLE001 - 单份失败不打断整批
                failed += 1
                print(f"  失败 {material.slug}：{exc}")
                continue

            total = len(lines)
            for slice_row in slices:
                start = max(1, int(slice_row.start_line or 1))
                end = min(total, int(slice_row.end_line or start))
                if end < start:
                    slice_row.text = ""
                    continue
                slice_row.text = "\n".join(lines[start - 1:end])
                chars += len(slice_row.text)
            session.commit()
            done += 1
            if done % 5 == 0 or index == len(mats):
                print(f"  {done} 份 / 已截 {chars / 1024 / 1024:.1f} MB 正文（{material.slug}）")

    print(f"完成：{done} 份截取，{skipped} 份跳过（没切片/没文件/已填），{failed} 份失败")
    print(f"正文合计约 {chars / 1024 / 1024:.1f} MB")
    return 0 if done else 1


if __name__ == "__main__":
    sys.exit(main())
