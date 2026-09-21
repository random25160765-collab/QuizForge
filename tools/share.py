#!/usr/bin/env python3
"""把一条会话打成**一个能发出去的网页** —— 命令行入口。

装配逻辑在 `api/app/share.py`。**界面上的分享按钮（导出接口 `format=html`）
走的是同一份**，所以命令行与界面出来的东西不可能不一样；这里只负责
"从库里取出这条会话 + 把结果落到磁盘"。

    make share                # 最近更新的那一条
    make share CID=<uuid>     # 指定会话

产物默认落在仓库根 `share-<id8>.html`，**一个文件**，`file://` 双击即用。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))

from app import share  # noqa: E402
from app.db import get_session_factory  # noqa: E402
from app.models import Conversation  # noqa: E402
from app.routers.chat import _message_out, _messages_of  # noqa: E402

WEB = ROOT / "api" / "web"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把一条会话打成可分享的单页 HTML")
    parser.add_argument("cid", nargs="?", default="latest", help="会话 id，或 `latest`（默认）")
    parser.add_argument("--out", default="", help="输出路径，默认 share-<id8>.html 放仓库根")
    args = parser.parse_args(argv)

    db = get_session_factory()()
    try:
        if args.cid == "latest":
            conv = db.query(Conversation).order_by(Conversation.updated_at.desc()).first()
        else:
            conv = db.get(Conversation, args.cid)
        if conv is None:
            print("✗ 没找到这条会话：%s" % args.cid, file=sys.stderr)
            return 1

        messages = [_message_out(m) for m in _messages_of(db, conv)]
        try:
            html, stats = share.build_single_page(WEB, share.payload_of(conv, messages))
        except (FileNotFoundError, share.ShareBuildError) as err:
            print("✗ %s" % err, file=sys.stderr)
            return 2
    finally:
        db.close()

    out = Path(args.out) if args.out else ROOT / ("share-%s.html" % str(conv.id)[:8])
    out.write_text(html, encoding="utf-8")

    print("✓ %s" % out)
    print(
        "  %s · %d 条消息 · 内联了 %d 份样式 / %d 个脚本（丢掉 boot.js %d 个）"
        % ((conv.title or "(无题)")[:40], len(messages), stats["css"], stats["js"], stats["boot"])
    )
    print("  %.1f MB —— 直接发出去，对方双击就能看" % (out.stat().st_size / 1048576))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
