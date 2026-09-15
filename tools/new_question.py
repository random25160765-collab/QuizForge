#!/usr/bin/env python3
"""出题脚手架：生成带正确 front-matter 与小节骨架的题目文件。

用法：
    python3 tools/new_question.py --topic cpp --type single --title 迭代器失效规则
    python3 tools/new_question.py --topic cuda-mem-shared --type short
    python3 tools/new_question.py --topic qemu-device-memregion -t single --difficulty 4

行为：
  * 自动分配该主题下未占用的四位编号（cpp-0007 之类）
  * 文件落在 questions/<topic>/<topic>-<编号>-<slug>.md
  * 已存在同名文件时报错退出，不覆盖
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from check import load_topics  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
QUESTIONS_DIR = ROOT / "questions"

SLUG_KEEP = re.compile(r"[^a-z0-9]+")

TYPE_SKELETONS = {
    "single": """## 选项
- A. 第一个选项
- B. 第二个选项
- C. 第三个选项

## 答案
B

## 解析
在这里写解析，可以混排公式与代码。
""",
    "multi": """## 选项
- A. 第一个选项
- B. 第二个选项
- C. 第三个选项
- D. 第四个选项

## 答案
A,C

## 解析
多选需要写清楚每个选项为什么对 / 为什么错。
""",
    "blank": """## 答案
可接受答案1|别名2

## 解析
「## 答案」里每行对应一个空；同一个空的多个可接受答案用 | 分隔；
以 ~ 开头的项按正则匹配，例如 ~^10\\^3$。
""",
    "short": """## 参考答案
在这里写完整的参考答案。

## 评分要点
- 要点一
- 要点二
- 要点三
""",
}


def slugify(text: str) -> str:
    """中文标题无法生成英文 slug 时回退到 'topic'。"""
    ascii_text = text.lower().encode("ascii", "ignore").decode("ascii")
    slug = SLUG_KEEP.sub("-", ascii_text).strip("-")
    return slug[:48] or "question"


def next_index(topic: str) -> int:
    """扫描该主题下已有的 <topic>-NNNN 编号，返回下一个可用值。"""
    directory = QUESTIONS_DIR / topic
    used = set()
    if directory.is_dir():
        pattern = re.compile(rf"^{re.escape(topic)}-(\d{{4}})")
        for path in directory.glob("*.md"):
            match = pattern.match(path.name)
            if match:
                used.add(int(match.group(1)))
    index = 1
    while index in used:
        index += 1
    return index


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="生成一道新题目的骨架文件")
    parser.add_argument("--topic", "-t", required=True, help="主题 key，需存在于 meta/topics.yaml")
    parser.add_argument("--type", dest="qtype", default="single", choices=sorted(TYPE_SKELETONS), help="题型")
    parser.add_argument("--title", default="", help="标题，用于生成文件名 slug 与题面首行")
    parser.add_argument("--difficulty", type=int, default=3, help="难度 1..5")
    parser.add_argument("--source", default="", help="出处")
    parser.add_argument("--id", dest="explicit_id", default="", help="显式指定 id（默认自动分配）")
    parser.add_argument("--force", action="store_true", help="允许覆盖已存在的文件")
    parser.add_argument("--print", dest="dry_run", action="store_true", help="只打印内容，不写文件")
    args = parser.parse_args(argv[1:])

    topics = load_topics()
    if args.topic not in topics:
        print(f"[ERROR] topic「{args.topic}」未在 meta/topics.yaml 中定义", file=sys.stderr)
        print("        已定义：" + "、".join(sorted(topics)), file=sys.stderr)
        return 2

    # 题目可以挂在主题树的任意一层，但文件与 id 一律按「学科」（一级）组织，
    # 否则 80 个知识点会散成 80 个目录。
    subject = topics[args.topic]["path"][0]

    index = next_index(subject)
    qid = args.explicit_id or f"{subject}-{index:04d}"
    if not re.match(r"^[a-z0-9]+(?:-[a-z0-9]+)*$", qid):
        print(f"[ERROR] id「{qid}」不合法，要求 kebab-case", file=sys.stderr)
        return 2

    slug = slugify(args.title) if args.title else "note"
    directory = QUESTIONS_DIR / subject
    path = directory / f"{qid}-{slug}.md"

    lines = ["---"]
    lines.append(f"id: {qid}")
    lines.append(f"type: {args.qtype}")
    lines.append(f"topic: {args.topic}")
    lines.append(f"difficulty: {max(1, min(5, args.difficulty))}")
    if args.source:
        lines.append(f'source: "{args.source}"')
    lines.append("---")
    lines.append("")
    lines.append(args.title if args.title else "在这里写题干，支持 Markdown 与 $LaTeX$ 公式。")
    lines.append("")
    lines.append(TYPE_SKELETONS[args.qtype].rstrip())
    lines.append("")

    content = "\n".join(lines)

    if args.dry_run:
        print(content)
        return 0

    if path.exists() and not args.force:
        print(f"[ERROR] 文件已存在：{path.relative_to(ROOT)}", file=sys.stderr)
        print("        如需覆盖请加 --force，或换一个 id。", file=sys.stderr)
        return 1

    directory.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    print(f"[OK] 已创建 {path.relative_to(ROOT)}")
    print(f"     题型 {args.qtype} · 主题 {topics[args.topic].get('name', args.topic)} · 难度 {args.difficulty}")
    print("     编辑完成后运行：python3 tools/check.py " + str(path.relative_to(ROOT)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
