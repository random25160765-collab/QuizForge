#!/usr/bin/env python3
"""检索的手感测试台：**打一句话，看回来什么、以及分是怎么来的**。

## 为什么要有它

检索质量的问题通常长这样："回来的东西里混进了完全无关的" —— 而**看**这件事以前只有
两条路：要么开对话（那里看不到 `cosine` 与 `titleMatch`，只看到一个名次分），要么现写
一段 python（下次还得再写一遍，结论留不下来）。这里固定成一条命令，并把三个数摊开：

* `cosine` —— 真实的余弦相似度，判"像不像"的唯一硬数；
* `titleMatch` —— 问句与**标题**的亲密度（0~1）。内容像不等于名字对得上：
  实测问"GPU 架构演变史"，一本讲 GPU 的书能到 0.7374，而标题就叫
  《GPU架构演化史》的那篇是 0.7776 —— 差 0.009，只有标题能把这两类分开；
* `score` —— 最终排序分（名次融合 RRF + 标题加成）。

## 用法

    python3 tools/search.py "zartbot GPU 架构演变史"
    python3 tools/search.py "虚拟内存分页" --limit 10
    python3 tools/search.py "causal mask" --slug helloalgozh   # 只在一份材料里找
    python3 tools/search.py "怎么切分页表" --depth 出题          # 只搜那一个书架

**只读**：不写库、不改任何东西。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "api"))

from app import local_embed, semantic  # noqa: E402
from app.db import get_session_factory  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tools/search.py", description="检索手感测试台（只读）")
    parser.add_argument("query", help="一句话或关键词")
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--slug", default="", help="只在这份材料里找")
    parser.add_argument("--depth", default="", help="只搜这一档：出题 / 检索")
    parser.add_argument("--per-material", type=int, default=semantic.PER_MATERIAL,
                        help="同一份材料最多占几格（默认 %d）" % semantic.PER_MATERIAL)
    args = parser.parse_args(argv)

    session = get_session_factory()()
    try:
        try:
            vec = local_embed.embed([args.query], kind="query")[0]
        except Exception as exc:  # noqa: BLE001
            # 本地小模型没就绪时只走字面那一路 —— 不假装，也不直接崩
            print("（向量那一路不可用：%s；只走字面）" % type(exc).__name__)
            vec = None
        out = semantic.search_fused(
            session, args.query, query_vec=vec, slug=args.slug,
            limit=args.limit, depth=args.depth, per_material=args.per_material,
        )
        if out.get("error"):
            print(out["error"])
            return 1
        hits = out.get("hits") or []
        print("问：%s" % args.query)
        print("扫了 %s/%s 份 · 回来 %d 条%s" % (
            out.get("scanned"), out.get("total"), len(hits),
            "" if out.get("semantic") else " · **向量那一路没参与**"))
        print("-" * 100)
        for rank, hit in enumerate(hits, 1):
            print("%2d. %-24s %s" % (
                rank, str(hit.get("material"))[:24], str(hit.get("title") or "")[:38]))
            print("    行 %s-%s · 余弦 %s · 标题亲密度 %s · 排序分 %s · 找到它的路：%s" % (
                hit.get("startLine"), hit.get("endLine"), hit.get("cosine"),
                hit.get("titleMatch"), hit.get("score"), hit.get("via")))
            snippet = str(hit.get("quote") or "").strip().replace("\n", " ")
            if snippet:
                print("    %s" % snippet[:96])
        if out.get("note"):
            print("\n%s" % out["note"])
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
