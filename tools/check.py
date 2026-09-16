#!/usr/bin/env python3
"""题库校验器。

在构建前把整个题库扫一遍，任何 ERROR 都会让构建失败，避免产出半损坏的 HTML。

校验项：
  [E] front-matter 必填字段（id / type / topic）
  [E] type 合法、topic 存在于 meta/topics.yaml
  [E] id 全局唯一，且与文件名同前缀、kebab-case
  [E] single/multi 的答案必须落在「## 选项」里，选项至少 2 个，multi 至少 2 个正确项
  [E] blank 答案非空；short 必须同时有「## 参考答案」与「## 评分要点」
  [E] 题面不能为空
  [W] LaTeX $ 定界符数量为奇数（可能漏写）
  [W] 代码围栏未配对
  [W] difficulty 越界或非整数（会被夹到 1..5）
  [W] tags 里的奇怪字符、chapter 缺失

用法：
    python3 tools/check.py            # 校验整个题库
    python3 tools/check.py path.md    # 校验单个文件（便于出题时快速自检）
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# 主题树解析（学科 → 单元 → 知识点），与 build.py 共用同一份
import topics as _topics  # noqa: E402
from question_parser import (  # noqa: E402
    QuestionParseError,
    iter_question_files,
    parse_question,
)

ROOT = Path(__file__).resolve().parent.parent
# 题库与考纲的**权威在数据库**，仓库里不再放"一题一个文件"的小文件。
# `make bank-materialize` 把库导出一个临时目录，下面两个环境变量指向它（与 Docker 同名）。
QUESTIONS_DIR = Path(os.environ.get("QF_QUESTIONS_DIR") or (ROOT / "questions"))
TOPICS_FILE = Path(os.environ.get("QF_TOPICS_FILE") or (ROOT / "meta" / "topics.yaml"))

_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_SECTION_RE = re.compile(r"^ {0,3}##\s+(.+?)\s*#*\s*$")
_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# 这些小节里的内容是给判分器看的（可能含 /^...$/ 这类正则），不参与 LaTeX 检查
_NO_TEX_SECTIONS = {"答案"}


@dataclass
class Diagnostic:
    level: str  # ERROR | WARN
    path: Path
    line: int
    message: str

    def render(self) -> str:
        try:
            shown = self.path.relative_to(ROOT)
        except ValueError:
            shown = self.path
        return f"{shown}:{self.line}: {self.level}: {self.message}"


def load_topics() -> dict[str, dict]:
    """返回扁平节点表（含 depth/parent/path/descendants 等层级字段）。

    解析逻辑放在 tools/topics.py，与 build.py 共用一份，避免两边漂移。
    """
    nodes, _ordered, _groups, problems = _topics.load()
    for problem in problems:
        print(f"[WARN] meta/topics.yaml: {problem}", file=sys.stderr)
    return nodes


def _check_tex_balance(path: Path, text: str, diags: list[Diagnostic]) -> None:
    """在代码围栏、行内码与「## 答案」小节之外统计 $ 的数量，奇数则告警。"""
    in_fence = False
    section = ""
    for index, line in enumerate(text.splitlines(), start=1):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        heading = _SECTION_RE.match(line)
        if heading:
            section = heading.group(1).strip()
            continue
        if section in _NO_TEX_SECTIONS:
            continue
        stripped = _INLINE_CODE_RE.sub("", line)
        # 去掉被转义的 \$
        count = len(re.findall(r"(?<!\\)\$", stripped))
        if count % 2:
            diags.append(
                Diagnostic(
                    "WARN",
                    path,
                    index,
                    f"本行有 {count} 个未转义的 $，数量为奇数，LaTeX 定界符可能漏写",
                )
            )


def _check_fences(path: Path, text: str, diags: list[Diagnostic]) -> None:
    in_fence = False
    fence_line = 0
    for index, line in enumerate(text.splitlines(), start=1):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            fence_line = index
    if in_fence:
        diags.append(Diagnostic("WARN", path, fence_line, "代码围栏没有闭合的 ```"))


def check_file(path: Path, topics: dict[str, dict], diags: list[Diagnostic]) -> None:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        diags.append(Diagnostic("ERROR", path, 1, f"读取失败：{exc}"))
        return

    try:
        question = parse_question(path)
    except QuestionParseError as exc:
        diags.append(Diagnostic("ERROR", exc.path, exc.line, str(exc).split(": ", 1)[-1]))
        return

    # ------------------------------------------------------------ 元数据
    if not question.id:
        diags.append(Diagnostic("ERROR", path, 1, "缺少必填字段 id"))
    elif not _ID_RE.match(question.id):
        diags.append(
            Diagnostic("ERROR", path, 1, f"id「{question.id}」不合法，要求 kebab-case（小写字母/数字/连字符）")
        )
    else:
        stem_prefix = path.parent.name
        if not question.id.startswith(f"{stem_prefix}-"):
            diags.append(
                Diagnostic(
                    "WARN",
                    path,
                    1,
                    f"id「{question.id}」未以目录名「{stem_prefix}-」开头，建议保持一致便于定位",
                )
            )
        # 目录按「学科」（一级）组织，而题目可以挂在任意一层，
        # 所以这里比对的是 topic 所属的一级学科，而不是 topic 本身。
        node = topics.get(question.topic)
        subject_key = node["path"][0] if node else question.topic
        if path.parent.name != subject_key:
            diags.append(
                Diagnostic(
                    "WARN",
                    path,
                    1,
                    f"文件所在目录「{path.parent.name}」与 topic「{question.topic}」所属学科「{subject_key}」不一致",
                )
            )

    if question.topic not in topics:
        known = "、".join(sorted(topics))
        diags.append(
            Diagnostic("ERROR", path, 1, f"topic「{question.topic}」未在 meta/topics.yaml 中定义（已知：{known}）")
        )

    if not question.stem.strip():
        diags.append(Diagnostic("ERROR", path, 1, "题面为空，请在小节「## 选项」之前写题干"))

    raw_difficulty = question.meta.get("difficulty", 3)
    try:
        value = int(raw_difficulty)
        if not 1 <= value <= 5:
            diags.append(Diagnostic("WARN", path, 1, f"difficulty={value} 超出 1..5，构建时会夹到边界值"))
    except (TypeError, ValueError):
        diags.append(Diagnostic("WARN", path, 1, f"difficulty={raw_difficulty!r} 不是整数，构建时按 3 处理"))

    if not question.meta.get("chapter"):
        diags.append(Diagnostic("WARN", path, 1, "建议填写 chapter，用于按章节分组与统计"))

    for tag in question.tags:
        if any(ch in tag for ch in "\n\r\t|"):
            diags.append(Diagnostic("WARN", path, 1, f"tag「{tag}」含异常字符"))

    # ------------------------------------------------------------ 分题型
    if question.type in ("single", "multi"):
        option_keys = {o.key for o in question.options}
        if len(question.options) < 2:
            diags.append(Diagnostic("ERROR", path, 1, f"{question.type} 题至少需要 2 个选项"))
        if question.type == "single":
            if question.answer and question.answer not in option_keys:
                diags.append(
                    Diagnostic(
                        "ERROR",
                        path,
                        1,
                        f"single 题答案「{question.answer}」不在选项 {sorted(option_keys)} 中",
                    )
                )
        else:
            bad = [a for a in (question.answer or []) if a not in option_keys]
            if bad:
                diags.append(
                    Diagnostic("ERROR", path, 1, f"multi 题答案 {bad} 不在选项 {sorted(option_keys)} 中")
                )
        if not question.explanation.strip():
            diags.append(Diagnostic("WARN", path, 1, f"{question.type} 题没有「## 解析」，建议补充"))

    elif question.type == "blank":
        for index, group in enumerate(question.answer or [], start=1):
            if not group and not (question.answer_regex[index - 1] if index - 1 < len(question.answer_regex) else None):
                diags.append(Diagnostic("ERROR", path, 1, f"blank 题第 {index} 个空没有任何可接受答案"))
        if not question.explanation.strip():
            diags.append(Diagnostic("WARN", path, 1, "blank 题没有「## 解析」，建议补充"))

    elif question.type == "short":
        if not question.reference.strip():
            diags.append(Diagnostic("ERROR", path, 1, "short 题缺少「## 参考答案」小节"))
        if not question.rubric:
            diags.append(Diagnostic("WARN", path, 1, "short 题没有「## 评分要点」，AI 批改会缺少 rubric"))

    elif question.type == "problem":
        if not question.parts:
            diags.append(Diagnostic("ERROR", path, 1, "problem 题没有任何小问"))
        titles = [p.title for p in question.parts]
        if len(set(titles)) != len(titles):
            diags.append(Diagnostic("WARN", path, 1, "存在同名小问，建议每问标题唯一"))
        for part in question.parts:
            if not part.stem.strip():
                diags.append(Diagnostic("ERROR", path, 1, f"第 {part.index} 问（{part.title}）的题干为空"))
            if not part.rubric:
                diags.append(
                    Diagnostic("WARN", path, 1, f"第 {part.index} 问（{part.title}）没有「#### 评分要点」")
                )
            if not part.reference.strip():
                diags.append(
                    Diagnostic("WARN", path, 1, f"第 {part.index} 问（{part.title}）没有「#### 参考答案」")
                )

    # blank 题的作答形态
    blank_mode = str(question.meta.get("blankMode", "") or "")
    if blank_mode and blank_mode not in ("inline", "code"):
        diags.append(Diagnostic("ERROR", path, 1, f"blankMode 只能是 inline 或 code，实际为 {blank_mode!r}"))
    if blank_mode == "code" and question.type != "blank":
        diags.append(Diagnostic("WARN", path, 1, "blankMode 只对 blank 题生效"))

    _check_tex_balance(path, raw, diags)
    _check_fences(path, raw, diags)


def main(argv: list[str]) -> int:
    if not TOPICS_FILE.is_file():
        print(f"[ERROR] 找不到主题定义文件：{TOPICS_FILE}", file=sys.stderr)
        return 2
    topics = load_topics()
    if not topics:
        print(f"[ERROR] {TOPICS_FILE} 里没有定义任何 topic", file=sys.stderr)
        return 2

    if len(argv) > 1:
        files = [Path(a).resolve() for a in argv[1:]]
    else:
        if not QUESTIONS_DIR.is_dir():
            print(f"[ERROR] 找不到题库目录：{QUESTIONS_DIR}", file=sys.stderr)
            return 2
        files = iter_question_files(QUESTIONS_DIR)

    diags: list[Diagnostic] = []
    seen: dict[str, Path] = {}

    for path in files:
        before = len(diags)
        check_file(path, topics, diags)
        # id 唯一性（只对解析成功的题目生效）
        try:
            qid = parse_question(path).id
        except QuestionParseError:
            qid = ""
        if qid:
            if qid in seen:
                diags.insert(
                    before,
                    Diagnostic(
                        "ERROR",
                        path,
                        1,
                        f"id「{qid}」与 {seen[qid].relative_to(ROOT)} 重复",
                    ),
                )
            else:
                seen[qid] = path

    errors = [d for d in diags if d.level == "ERROR"]
    warns = [d for d in diags if d.level == "WARN"]

    for diag in sorted(diags, key=lambda d: (str(d.path), d.line, d.level)):
        print(diag.render())

    print()
    if errors:
        print(f"[FAIL] {len(files)} 个文件：{len(errors)} 个错误，{len(warns)} 个告警")
        return 1
    print(f"[OK] {len(files)} 个文件全部通过（{len(warns)} 个告警）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
