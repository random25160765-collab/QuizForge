"""归一 + 切片（**当前只支持 Markdown**；PDF / HTML / 源码将来各加一个前端）。

为什么要有这一步、以及为什么格式相关的东西只允许出现在这一步：

* 下游（抽取 / 出题 / 校验 / 提升）全都建立在一个假设上 —— **材料是一份带稳定行号的纯文本**。
  换格式时如果让这个假设漏进链路，每个 agent、每条提示词都要分叉。
* 所以：**入口归一，链上无感**。切片记下原文行区间（唯一不可妥协的是出处坐标），
  PDF 之类只要在这里把坐标归一好（含页↔行映射），别的环节一行不改。

切的是**结构**（Markdown 标题），不是长度；长度只做兜底约束（太长的拆、太碎并）。
产物与 `dispatch` 读的三件套一致：`material.yaml` / `slices.yaml` / `figures.yaml`。

用法（路径给**仓库外**那份材料的实际位置，谁在哪都不影响）：
    api/.venv/bin/python -m pipeline.ingest ~/Source/tt-metal/METALIUM_GUIDE.md
    api/.venv/bin/python -m pipeline.ingest ~/Source/tt-metal/tech_reports --glob "**/*.md"
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

import yaml

from . import config

HEADING_RE = re.compile(r"^(#{2,3})\s+(.+?)\s*$", re.M)
IMG_RE = re.compile(r"""<img[^>]*?src=["']([^"']+)["'][^>]*>""", re.I)
MD_IMG_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.M)

MIN_LINES = 40       # 比这更短的片并进下一片
MAX_LINES = 400      # 比这更长的片按行拆开
SPLIT_AT = 250       # 长片切分点


def estimate_tokens(text: str) -> int:
    """粗估 token：中英混排按 ≈3.6 字符/token。只用来给任务分档，不需要很准。"""
    return max(1, round(len(text) / 3.6))


def slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "material"


def split_slices(lines: list[str]) -> list[tuple[int, int, str]]:
    """按 `##`/`###` 标题切；返回 [(起行, 止行, 标题), ...]，行号 1-based 闭区间。"""
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

    # 太碎的并进下一片；太长的按行拆开
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


def collect_figures(lines: list[str], ref: str) -> list[dict]:
    """收集图：块级 `<img src>` 与行内 `![](...)`。caption 取 alt/最近的非空文本行。"""
    figures: list[dict] = []
    for index, line in enumerate(lines):
        src = None
        caption = ""
        match = IMG_RE.search(line)
        if match:
            src = match.group(1)
        else:
            match = MD_IMG_RE.search(line)
            if match:
                caption, src = match.group(1).strip(), match.group(2).strip()
        if not src:
            continue
        if not caption:
            for back in range(index - 1, max(-1, index - 4), -1):
                text = lines[back].strip()
                if text and not text.startswith(("<", "|", "!")):
                    caption = text[-120:]
                    break
        figures.append(
            {
                "id": f"fig{len(figures) + 1:03d}",
                "src": src,
                "loc": {"lines": [index + 1, index + 1]},
                "caption": caption,
                "lead": "",
            }
        )
    return figures


def ingest_one(path: Path, subject: str, base: Path | None = None) -> Path:
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    name = slug(str(path.relative_to(base)).replace("/", "-").removesuffix(".md")) if base else slug(path.stem)
    slices = []
    for order, (start, end, title) in enumerate(split_slices(lines), start=1):
        body = "\n".join(lines[start - 1 : end])
        figures = collect_figures(lines[start - 1 : end], "")
        for fig in figures:  # 图的行号要还原成全文坐标
            fig["loc"]["lines"] = [fig["loc"]["lines"][0] + start - 1, fig["loc"]["lines"][1] + start - 1]
        slices.append(
            {
                "id": f"sl-{order:03d}",
                "path": f"{path.name}#L{start}-L{end}",
                "loc": {"lines": [start, end]},
                "tokens": estimate_tokens(body),
                "figures": [fig["id"] for fig in figures],
                "summary": title,
            }
        )
    figures_all = collect_figures(lines, "")
    for fig in figures_all:
        fig["slice"] = next(
            (s["id"] for s in slices if s["loc"]["lines"][0] <= fig["loc"]["lines"][0] <= s["loc"]["lines"][1]),
            slices[0]["id"] if slices else "",
        )
        fig["keys"] = []
        fig["kind"] = ""

    h1 = H1_RE.search(text)
    # **直接入库**：切片是抽取的作业单位，派工要问"哪些切片还没抽过"、归并要知道
    # "片与片是否同节" —— 这些只有在库里才成立。切片与图清单不再落 `maps/`。
    from . import dbstore  # noqa: PLC0415

    info = dbstore.save_slices(
        subject,
        {
            "material": name,
            "subject": subject,
            "source_path": str(path),
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "lines": len(lines),
            "title": (h1.group(1) if h1 else path.stem),
        },
        slices,
        figures_all,
    )
    info["tokens"] = sum(s["tokens"] for s in slices)
    return info


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.ingest", description="归一 + 切片（Markdown）")
    parser.add_argument("path", help="一个 .md 文件或一个目录")
    parser.add_argument("--subject", default="tt-metal", help="归到哪个学科目录下（maps/<subject>/）")
    parser.add_argument("--glob", default="*.md", help="目录模式下的匹配式（默认只扫当前层）")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 个（试点用）")
    args = parser.parse_args(argv)

    target = Path(args.path).expanduser()
    if target.is_file():
        files, base = [target], None
    elif target.is_dir():
        files, base = sorted(target.glob(args.glob)), target
    else:
        raise SystemExit(f"路径不存在：{target}")
    if args.limit:
        files = files[: args.limit]
    if not files:
        raise SystemExit(f"没有匹配到文件：{target} --glob {args.glob}")

    total_slices = total_lines = 0
    for path in files:
        info = ingest_one(path, args.subject, base)
        total_slices += info["slices"]
        total_lines += info["lines"]
        print(
            f"  {info['material']:44s} {info['slices']:3d} 片 · {info['tokens']:6d} tokens"
            f" · {info['figures']} 张图"
        )
    print(f"\n归一 {len(files)} 个文件 → {total_slices} 片 · {total_lines} 行 · **已入库**")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
