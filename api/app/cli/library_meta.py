"""用模型判（或重判）资料元数据。

    python -m app.cli.library_meta                 # 判还没判过的（origin 不是 llm/manual）
    python -m app.cli.library_meta --all           # 全判一遍
    python -m app.cli.library_meta --only <citekey> [--only <citekey> ...]
    python -m app.cli.library_meta --dry-run       # 只打印会改成什么，不落盘
    python -m app.cli.library_meta --limit 5       # 这一轮最多判几条

为什么要有这个命令（不是都走界面上那个"用模型重判"按钮吗）：

界面上那个按钮走**服务进程**，而这个命令走**你现在的这台机器**。实测两者不是一回事 ——
本机开发时服务是被沙箱起来的，`/api/ai/ping` 会一直挂到超时，而这个命令直连模型一秒一条。
所以"把库里几十条糊弄过的元数据修一遍"这种一次性的事，用命令更快也更好看重判了什么。

判据与界面按钮**完全同一套代码**（`library_meta.infer`）：命令只是多了一层批量与打印。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from .. import ai_gateway as gateway
from .. import library as lib
from .. import library_meta
from ..config import get_settings
from ..db import get_session_factory
from ..settings_store import row as settings_row
from ..routers import library as libapi


def _config() -> dict[str, Any]:
    """这次用哪套模型配置：**内测通道 > 某个用户自己的密钥**。

    命令没有"当前请求"，所以拿不到 `resolve_config` 的入参。这里按同样的优先序
    自己走一遍：内测通道开着就用它（这是开发机上的常态），否则取库里第一行设置。
    """
    beta = gateway.beta_config()
    if beta and beta.get("apiKey"):
        return {
            "apiKey": str(beta["apiKey"]),
            "baseUrl": str(beta.get("baseUrl") or gateway.DEFAULT_BASE_URL),
            "model": str(beta.get("model") or gateway.DEFAULT_MODEL),
            "timeoutMs": int(beta.get("timeoutMs") or get_settings().ai_timeout_ms),
            "jsonMode": bool(beta.get("jsonMode")),
            "source": "beta",
        }
    session = get_session_factory()()
    try:
        row = settings_row(session)
        conf = ((row.data if row else {}) or {}).get("ai") or {}
    finally:
        session.close()
    if not (conf.get("enabled") and str(conf.get("apiKey") or "").strip()):
        raise SystemExit(
            "  没有可用的模型配置：内测通道没开，库里的用户设置也没填密钥。\n"
            "  去「设置 → AI」填一个，或让站长配好 config/ai.local.json。"
        )
    return {
        "apiKey": str(conf["apiKey"]),
        "baseUrl": str(conf.get("baseUrl") or gateway.DEFAULT_BASE_URL),
        "model": str(conf.get("model") or gateway.DEFAULT_MODEL),
        "timeoutMs": int(conf.get("timeoutMs") or get_settings().ai_timeout_ms),
        "jsonMode": bool(conf.get("jsonMode")),
        "source": "user",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli.library_meta", description="用模型判资料元数据")
    parser.add_argument("--all", action="store_true", help="连已经判过的也重判")
    parser.add_argument("--only", action="append", default=[], help="只判这个 citekey（可给多次）")
    parser.add_argument("--limit", type=int, default=0, help="这一轮最多判几条（0 = 不限）")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不落盘")
    parser.add_argument(
        "--concurrency", type=int, default=library_meta.DEFAULT_WORKERS, help="并发宽度（默认 6）"
    )
    args = parser.parse_args(argv)

    conf = _config()
    print(f"  模型：{conf['model']}（{conf['source']}）")

    meta_dir, text_dir = libapi._meta_dir(), libapi._text_dir()
    roots = [Path(part) for part in libapi.default_roots()]
    only = set(args.only)
    # 先挑出这一轮要判的，再**并发**问模型 —— 库里几百条时串行是几分钟的事
    picked: list[tuple[Any, dict[str, Any], str]] = []
    for entry in lib.entries(roots, meta_dir, text_dir):
        meta = dict(entry.meta or {})
        key = entry.citekey
        if only and key not in only:
            continue
        origin = str(meta.get("origin") or "")
        if not args.all:
            # 与界面上那个按钮同一条判据：人手工补过的（manual）不动
            if origin == "manual" or not library_meta.looks_unjudged(meta):
                continue
        if args.limit and len(picked) >= args.limit:
            break
        picked.append((entry, meta, lib.head_text_for(entry.item, text_dir, key)))

    total = len(picked)
    print(f"  要判 {total} 条，并发 {args.concurrency}")
    done = failed = 0
    # 进度条：**每完成一条就重画**。并发的意义是快，看不见进度就等于没并发
    # （用户："卡死了，你的并发真的有并发吗？做个进度条吧"）。
    line = {"open": False}

    def progress(at: int, whole: int, result: tuple[Any, Any, str]) -> None:
        filled = int(round(28 * at / max(1, whole)))
        bar = "█" * filled + "·" * (28 - filled)
        mark = "✓" if result[1] is not None else "✗"
        sys.stdout.write(f"\r  [{bar}] {at}/{whole} {mark}")
        sys.stdout.flush()
        line["open"] = at < whole
        if at >= whole:
            sys.stdout.write("\n")

    judged = library_meta.judge_many(
        [(entry.item, head) for entry, _meta, head in picked],
        conf,
        workers=args.concurrency,
        on_done=progress,
    )
    if line["open"]:
        sys.stdout.write("\n")
    for (entry, meta, _head), (_item, fresh, why) in zip(picked, judged, strict=False):
        key = entry.citekey
        if fresh is None:
            failed += 1
            print(f"  ✗ {key}\n      {why[:140]}")
            continue
        done += 1
        old_title = str(meta.get("title") or "")
        if not args.dry_run:
            for field in ("title", "authors", "year", "kind", "topics"):
                if fresh.get(field):
                    meta[field] = fresh[field]
                elif field in ("authors", "topics"):
                    meta[field] = []
            meta["origin"] = "llm"
            meta["metaRev"] = library_meta.META_REV
            meta["citekey"] = key            # 身份不动：笔记里可能已经引用了
            meta.pop("why", None)
            lib.save_metadata(meta_dir, meta, origin="llm")
        print(f"  · {key}")
        if old_title and old_title != fresh["title"]:
            print(f"      标题：{old_title[:58]}")
            print(f"        →  {fresh['title'][:58]}")
        print(f"      作者：{fresh['authors']} | {fresh['year']} | {fresh['kind']} | {fresh['topics']}")
    print(("  （干跑，没有写）" if args.dry_run else "  已写回") + f"：成功 {done} 条，失败 {failed} 条")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
