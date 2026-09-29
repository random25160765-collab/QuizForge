#!/usr/bin/env python3
"""仓库里**我们自己写的代码**有多少行 —— 按语言、按目录数一遍。

## 为什么要固化成工具

这个数以前是现场敲的：`find . -name '*.py' | xargs wc -l`。问题不在打字，在**口径** ——
`vendor/` 里的第三方代码算不算、`api/web` 这个构建产物算不算、注释与空行算不算，
随手一敲每次都不一样，于是同一个问题每次得到不同的答案，而结论留不下来
（和 `tools/db_ops.py` 是同一个理由：写一次扔一次的代价是**没有共识**）。

所以：口径钉在代码里，并且**每次跑都打出来**。这里只读、不写任何东西。

## 口径

* 只数仓库里自己写的源码。**不算**：`vendor/`（第三方库、字体、Pyodide）、
  `api/web`（前端构建产物）、`api/var`（用户附件）、`data/`、`db/`、`build/dist`、
  `reference/`（仓库外的只读软链）、`.git-rewrite/`（整份仓库的副本）、
  仓库根的 `share-*.html`（`make share` 的产物）—— 列在 `SKIP_PATHS` /
  `SKIP_FILE_GLOBS` / `SKIP_NAMES` 里。**判断依据是"人写的还是生成的"**，
  不是"在不在 git 里"（`meta/topics.yaml` 在 git 里但只是配置，默认不数）。
* 空行不计；**整行注释**不计（`#`、`//`、`/* */`、`<!-- -->`）。
* **行尾注释不单算**：`x = 1  # 解释` 仍是代码行。逐行去注释要解析语法，
  收益不值那个代价（要精确就得用各语言的解析器，那是另一件事）。
* **三引号文档字符串算代码行**（与 cloc 同口径）。本仓库 `.py` 的文件头说明很长，
  想知道它占多少，看 `--by-dir` 里 `pipeline` 那几行的规模即可。
* `.pyc`、字体、数据、图片本来就不在后缀表里，走不到读文件那一步。

## 用法

    python3 tools/loc.py                  # 按语言汇总
    python3 tools/loc.py --by-dir         # 按目录汇总（默认两层：theme/runtime、api/app …）
    python3 tools/loc.py --by-dir --depth 1
    python3 tools/loc.py --path api       # 只看 api/ 这棵子树
    python3 tools/loc.py --all            # 连配置文件（json / yaml / toml / ini）一起数
    python3 tools/loc.py --json           # 给程序读（结构与打印无关）
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 注释风格。决定"整行注释"怎么认 —— 只认**行首**标记，行内的不追。
HASH = "hash"    # # 一直到行尾（Python / Shell / Makefile / YAML / TOML / INI）
SLASH = "slash"  # // 行注释 + /* */ 块注释（JavaScript / CSS）
HTMLC = "html"   # <!-- --> 块注释（HTML）
QUOTE = "quote"  # ' 一直到行尾（VBScript）

#: 后缀 → (语言名, 注释风格)。同一个语言的多个后缀合并计数。
SUFFIX_LANG: dict[str, tuple[str, str]] = {
    ".py": ("Python", HASH),
    ".js": ("JavaScript", SLASH),
    ".mjs": ("JavaScript", SLASH),
    ".css": ("CSS", SLASH),
    ".html": ("HTML", HTMLC),
    ".sh": ("Shell", HASH),
    ".vbs": ("VBScript", QUOTE),
}

#: 无后缀的文件按名字认（`Makefile` 是仓库里唯一一个）。
NAME_LANG: dict[str, tuple[str, str]] = {
    "Makefile": ("Makefile", HASH),
}

#: `--all` 才数进来的配置：它们不是程序，但也是人写的（CI、依赖、清单）。
CONFIG_SUFFIX: dict[str, tuple[str, str]] = {
    ".json": ("JSON", HASH),
    ".yaml": ("YAML", HASH),
    ".yml": ("YAML", HASH),
    ".toml": ("TOML", HASH),
    ".ini": ("INI", HASH),
    ".cfg": ("INI", HASH),
}

#: 按**名字**跳过的目录（任何层级）：缓存与虚拟环境，里面没有自写源码。
SKIP_NAMES = {
    "__pycache__",
    ".git",
    "node_modules",
    ".venv",
    "venv",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
}

#: 按**仓库相对路径**跳过的目录：第三方、构建产物、数据、仓库外的软链、工具的运行状态。
#: 用路径而不是名字，是因为 `web` 这种名字太通用（要跳的是 `api/web`，不是任何叫 web 的）。
SKIP_PATHS = (
    "vendor",
    "api/web",
    "api/var",
    "build/dist",
    "build/.pyinstaller",
    "data",
    "db",
    "maps",
    "reference",
    "dist",
    "dist-local",
    ".playwright-cli",
    ".bank-cache",      # 题库物化的临时目录（用完即删的副本）
    ".git-rewrite",     # git filter-branch 的中间目录：**整份仓库的副本**，数它等于数两遍
    ".codebuddy/plans",     # agent 的会话状态，不是项目代码
    ".codebuddy/teams",
    ".codebuddy/integration",
    ".codebuddy/sandbox",
)

#: 按**文件名**跳过的产物（`make share` 生成的单会话页面，几 MB 一个，在仓库根）。
SKIP_FILE_GLOBS = ("share-*.html",)


@dataclass
class Stat:
    """一组文件的四个数。`code` 是算出来的，不单独存 —— 免得两者打架。"""

    files: int = 0
    total: int = 0
    blank: int = 0
    comment: int = 0

    @property
    def code(self) -> int:
        return self.total - self.blank - self.comment

    def add(self, other: "Stat") -> None:
        self.files += other.files
        self.total += other.total
        self.blank += other.blank
        self.comment += other.comment


# ---------------------------------------------------------------- 认文件与数行


def skipped(rel: Path) -> bool:
    """这个（相对仓库根的）目录该不该整棵跳过。"""
    parts = rel.parts
    if any(part in SKIP_NAMES for part in parts):
        return True
    joined = "/".join(parts)
    return any(joined == path or joined.startswith(path + "/") for path in SKIP_PATHS)


def language_of(name: str, *, include_config: bool) -> tuple[str, str] | None:
    """文件名 → (语言名, 注释风格)；不是要数的文件就给 None。"""
    if any(fnmatch.fnmatch(name, pattern) for pattern in SKIP_FILE_GLOBS):
        return None
    if name in NAME_LANG:
        return NAME_LANG[name]
    suffix = Path(name).suffix.lower()
    if suffix in SUFFIX_LANG:
        return SUFFIX_LANG[suffix]
    if include_config and suffix in CONFIG_SUFFIX:
        return CONFIG_SUFFIX[suffix]
    return None


def classify(lines: list[str], style: str) -> tuple[int, int]:
    """→ (空行数, 注释行数)。只认行首标记，行尾注释不追。"""
    blank = 0
    comment = 0
    if style in (HASH, QUOTE):
        mark = "#" if style == HASH else "'"
        for line in lines:
            stripped = line.strip()
            if not stripped:
                blank += 1
            elif stripped.startswith(mark):
                comment += 1
        return blank, comment

    if style == SLASH:
        open_mark, close_mark, line_mark = "/*", "*/", "//"
    else:  # HTMLC
        open_mark, close_mark, line_mark = "<!--", "-->", None

    in_block = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            blank += 1
            continue
        if in_block:
            comment += 1
            if close_mark in stripped:
                in_block = False
            continue
        if line_mark is not None and stripped.startswith(line_mark):
            comment += 1
        elif stripped.startswith(open_mark):
            comment += 1
            # 注意是从 open_mark 之后找 close_mark：`/* a */` 里的 `*/` 不该被当成另一次
            if close_mark not in stripped[len(open_mark):]:
                in_block = True
    return blank, comment


def dir_key(rel: Path, depth: int) -> str:
    """文件 → 归到哪一行目录（两层是默认：`theme/runtime`、`api/app`…）。"""
    parts = rel.parts[:-1]
    if not parts:
        return "(根)"
    return "/".join(parts[:depth])


def scan(root: Path, *, depth: int, include_config: bool) -> tuple[dict[str, Stat], dict[str, Stat]]:
    """走一遍 root → (按语言, 按目录)。"""
    by_lang: dict[str, Stat] = {}
    by_dir: dict[str, Stat] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        rel_dir = here.relative_to(root)
        # 就地裁剪：跳过的目录连进都不进（reference/ 是仓库外的软链，走进去就是灾难）
        dirnames[:] = sorted(d for d in dirnames if not skipped(rel_dir / d))
        for name in filenames:
            found = language_of(name, include_config=include_config)
            if found is None:
                continue
            language, style = found
            try:
                text = (here / name).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue  # 读不了就不算，不为一个文件中断整次统计
            lines = text.splitlines()
            blank, comment = classify(lines, style)
            stat = Stat(files=1, total=len(lines), blank=blank, comment=comment)
            by_lang.setdefault(language, Stat()).add(stat)
            by_dir.setdefault(dir_key(rel_dir / name, depth), Stat()).add(stat)
    return by_lang, by_dir


# ---------------------------------------------------------------- 打印


def width(text: str) -> int:
    """显示宽度：中日韩字符占两格（不然表头对不齐）。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def cell(text: str, w: int, *, right: bool = False) -> str:
    gap = " " * max(0, w - width(text))
    return gap + text if right else text + gap


def num(value: int) -> str:
    return f"{value:,}"


def render(headers: list[str], rows: list[list[str]], total: list[str] | None) -> str:
    """一张表。第一列左对齐，其余右对齐（数字右对齐才读得出量级）。"""
    widths = [max(width(headers[i]), *(width(row[i]) for row in rows)) for i in range(len(headers))]
    line = "-" * (sum(widths) + 2 * (len(widths) - 1))
    out = ["  ".join(cell(h, widths[i], right=i > 0) for i, h in enumerate(headers)), line]
    out += ["  ".join(cell(v, widths[i], right=i > 0) for i, v in enumerate(row)) for row in rows]
    if total is not None:
        out += [line, "  ".join(cell(v, widths[i], right=i > 0) for i, v in enumerate(total))]
    return "\n".join(out)


def rows_by_lang(by_lang: dict[str, Stat]) -> list[list[str]]:
    ordered = sorted(by_lang.items(), key=lambda kv: (-kv[1].code, kv[0]))
    return [[name, num(s.files), num(s.total), num(s.blank), num(s.comment), num(s.code)]
            for name, s in ordered]


def rows_by_dir(by_dir: dict[str, Stat]) -> list[list[str]]:
    ordered = sorted(by_dir.items(), key=lambda kv: (-kv[1].code, kv[0]))
    return [[name, num(s.files), num(s.total), num(s.code)] for name, s in ordered]


def total_row(stat: Stat, columns: int) -> list[str]:
    """合计行：列数与表头对齐，中间补空。"""
    if columns == 6:
        return ["合计", num(stat.files), num(stat.total), num(stat.blank), num(stat.comment), num(stat.code)]
    return ["合计", num(stat.files), num(stat.total), num(stat.code)]


def main() -> int:
    parser = argparse.ArgumentParser(description="数一数仓库里自写代码的行数（只读）")
    parser.add_argument("--by-dir", action="store_true", help="按目录汇总而不是按语言")
    parser.add_argument("--depth", type=int, default=2, help="--by-dir 归到第几层（默认 2）")
    parser.add_argument("--all", action="store_true", help="连配置文件（json / yaml / toml / ini）一起数")
    parser.add_argument("--path", metavar="SUBDIR", help="只看这个子树（相对仓库根）")
    parser.add_argument("--json", action="store_true", help="输出 JSON，给程序读")
    args = parser.parse_args()

    root = ROOT
    label = "仓库根"
    if args.path:
        root = (ROOT / args.path).resolve()
        if not root.is_dir():
            print(f"没有这个目录：{args.path}", file=sys.stderr)
            return 1
        # 排除规则是按**仓库相对路径**写的，所以不管从哪棵子树起步都要换算回去
        try:
            label = str(root.relative_to(ROOT))
        except ValueError:
            print(f"{args.path} 不在仓库里（排除规则按仓库相对路径写的，这里用不上）", file=sys.stderr)
            return 1

    by_lang, by_dir = scan(root, depth=max(1, args.depth), include_config=args.all)
    overall = Stat()
    for stat in by_lang.values():
        overall.add(stat)

    if args.json:
        print(json.dumps({
            "root": label,
            "include_config": args.all,
            "total": {
                "files": overall.files, "lines": overall.total,
                "blank": overall.blank, "comment": overall.comment, "code": overall.code,
            },
            "languages": {name: vars(s) | {"code": s.code} for name, s in by_lang.items()},
            "dirs": {name: vars(s) | {"code": s.code} for name, s in by_dir.items()},
        }, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    print(f"quizforge · 代码量（{label}）")
    print()
    if args.by_dir:
        print(render(["目录", "文件", "总行", "代码"], rows_by_dir(by_dir), total_row(overall, 4)))
    else:
        print(render(["语言", "文件", "总行", "空行", "注释", "代码"],
                     rows_by_lang(by_lang), total_row(overall, 6)))
    print()
    print("口径：只数仓库里自写的源码（vendor/ 与构建产物不算）；不计空行与整行注释；"
          "行尾注释不单算，三引号文档字符串算代码。")
    if not args.all:
        print("      配置类（json / yaml / toml / ini）默认不数，要连它们一起看加 --all。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
