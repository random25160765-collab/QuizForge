"""把资料库里的文件**批量解析成行文本**入库（随后 `make sync` 就把正文推到云端）。

    命令行用法（在 `api/` 下）：

        python -m app.cli.library_docs            # 遍历设置里的资料根
        python -m app.cli.library_docs --dry-run   # 只看会处理哪些，不写库

新拖进来的文件不用等这条命令：`POST /api/library/upload` 落盘后当场入库
（见 `routers/library.py`）。这条是给**存量材料**补一次的。

为什么正文要进库：切片只存「原文件 + 行区间」，chat 的 `read_material` 原先
必须在本机找到那个文件。入库之后正文随 sync 上云，换台机器也能读，而原文件
仍然不用传（用户 2026-11-08 定的边界）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .. import library as lib
from .. import library_docs, settings_store
from ..db import get_session_factory
from ..routers.library import roots_for


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把资料文件解析成行文本入库")
    parser.add_argument("--dry-run", action="store_true", help="只列出来，不写库")
    parser.add_argument("--limit", type=int, default=0, help="最多处理几份（调试用）")
    args = parser.parse_args(argv)

    done = 0
    skipped = 0
    failed: list[tuple[str, str]] = []

    with get_session_factory()() as session:
        stored = settings_store.row(session)
        roots = roots_for(stored)
        if not roots:
            print("还没有资料根：先在「资料」页挂一个目录，或者上传一份文件。")
            return 2

        for root in roots:
            base = Path(root).expanduser()
            if not base.is_dir():
                print(f"跳过（目录不存在）：{base}")
                continue
            for path in lib.walk(base):
                if args.limit and done >= args.limit:
                    break
                if args.dry_run:
                    print(f"[dry] {path.relative_to(base)}")
                    done += 1
                    continue
                try:
                    info = library_docs.ingest(
                        session, path, title=lib.clean_title(path.stem) or path.stem
                    )
                except Exception as exc:  # noqa: BLE001 - 单份失败不打断整批
                    failed.append((str(path.relative_to(base)), str(exc)))
                    continue
                done += 1
                if done % 10 == 0:
                    print(f"  {done} 份…（最近：{info['slug']} {info['lines']} 行）")
    print(f"完成：{done} 份入库，{skipped} 份跳过，{len(failed)} 份失败")
    for rel, why in failed[:10]:
        print(f"  失败 {rel}：{why}")
    if failed:
        print("  （失败的多半是扫描版 PDF：没有文本层。原文件已经在了，不影响使用）")
    return 0 if done else 1


if __name__ == "__main__":
    sys.exit(main())
