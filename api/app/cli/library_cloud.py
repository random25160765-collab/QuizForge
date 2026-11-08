"""资料切片整包上传/下载。

        make library-push     # 把材料 + 切片 + 图推到云端
        make library-pull     # 从云端拉回来（按 slug / slice_id 覆盖本地）
        make library-show     # 只看本地这一包有多大、几行

只传切片，不传原文件、不重新解析（用户 2026-11-08 定的边界）。
"""

from __future__ import annotations

import argparse
import sys

from .. import library_cloud
from ..db import get_session_factory


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="资料切片整包同步")
    parser.add_argument("action", choices=("push", "pull", "show"), nargs="?", default="show")
    args = parser.parse_args(argv)

    with get_session_factory()() as session:
        try:
            if args.action == "push":
                out = library_cloud.push(session)
                print(
                    f"已上传：材料 {out['materials']} · 切片 {out['slices']} · 图 {out['figures']}"
                    f" · {out['bytes'] / 1024:.0f} KB"
                )
            elif args.action == "pull":
                out = library_cloud.pull(session)
                if out.get("empty"):
                    print("云端还没有资料包（先在任何一台机器上跑 `make library-push`）")
                    return 1
                print(
                    f"已拉取：材料 {out['materials']}（新增 {out['added']} 更新 {out['updated']}）"
                    f" · 切片 {out['slices']} · 图 {out['figures']}"
                )
            else:
                data = library_cloud.pack(session)
                size = len(str(data).encode("utf-8"))
                print(
                    f"本地一包：材料 {len(data['materials'])} · 切片 {len(data['slices'])}"
                    f" · 图 {len(data['figures'])} · 约 {size / 1024:.0f} KB"
                )
        except RuntimeError as exc:
            print(exc)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
