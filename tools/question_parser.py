#!/usr/bin/env python3
"""题目文件解析器。

一个题目文件 = YAML front-matter + Markdown 正文，正文用固定的二级小节
标记切分出「选项 / 答案 / 解析 / 参考答案 / 评分要点 / 提示」。

    ---
    id: cpp-0001
    type: single
    topic: cpp
    ---
    题面 Markdown

    ## 选项
    - A. 第一个选项
    - B. 第二个选项

    ## 答案
    B

    ## 解析
    这里是解析，可以写 $O(n\\log n)$。

解析要点：
  * 只认「行首（最多 3 个空格缩进）的 ## 标题」，且**不在围栏代码块内**；
    这样题面里的 ``` 代码块中出现 `## 注释` 不会被误判为小节。
  * front-matter 必须从文件第 1 行开始，以 --- 起、以 --- 或 ... 止。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------- 常量定义

VALID_TYPES = ("single", "multi", "blank", "short", "problem")

TYPE_LABELS = {
    "single": "单选",
    "multi": "多选",
    "blank": "填空",
    "short": "简答",
    "problem": "大题",
}

# 题面正文里允许出现的二级小节（顺序即展示顺序）
SECTION_ORDER = ("提示", "选项", "答案", "参考答案", "评分要点", "小问", "解析")

# 大题（type: problem）内部的小节标记
PART_HEADING_RE = re.compile(r"^ {0,3}###\s+(.+?)\s*#*\s*$")
PART_SUB_RE = re.compile(r"^ {0,3}####\s+(.+?)\s*#*\s*$")
_PART_SUB_KINDS = (("参考", "reference"), ("评分", "rubric"), ("提示", "hint"))

# 正则前缀：答案前加 ~ 表示按正则匹配
REGEX_PREFIX = "~"

_FRONT_MATTER_FENCE = re.compile(r"^(---|\.\.\.)\s*$")
_SECTION_RE = re.compile(r"^ {0,3}##\s+(.+?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_OPTION_RE = re.compile(r"^\s*[-*+]\s+(?:\(?([A-Za-z])\)?\s*[.、):：]\s*)?(.*)$")


class QuestionParseError(Exception):
    """题目文件语法错误，携带文件与行号便于定位。"""

    def __init__(self, path: Path, line: int, message: str) -> None:
        self.path = path
        self.line = line
        super().__init__(f"{path}:{line}: {message}")


@dataclass
class Option:
    key: str
    text: str


@dataclass
class ProblemPart:
    """大题（type: problem）的一个小问。"""

    index: int
    title: str
    stem: str = ""
    reference: str = ""
    rubric: list[str] = field(default_factory=list)
    hint: str = ""


@dataclass
class Question:
    path: Path
    meta: dict[str, Any]
    stem: str = ""
    sections: dict[str, str] = field(default_factory=dict)
    options: list[Option] = field(default_factory=list)
    parts: list[ProblemPart] = field(default_factory=list)
    # single -> "B" | multi -> ["A","C"] | blank -> [["1e3","1000"], ["O(n)"]] | short -> None
    answer: Any = None
    # blank 题对每个空的正则匹配串（与 answer 下标对应，None 表示该项按字面比较）
    answer_regex: list[list[str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # ---------------------------------------------------------- 便捷属性
    @property
    def id(self) -> str:
        return str(self.meta.get("id", ""))

    @property
    def type(self) -> str:
        return str(self.meta.get("type", ""))

    @property
    def topic(self) -> str:
        return str(self.meta.get("topic", ""))

    @property
    def tags(self) -> list[str]:
        raw = self.meta.get("tags") or []
        if isinstance(raw, str):
            raw = [t.strip() for t in re.split(r"[,，]", raw) if t.strip()]
        return [str(t) for t in raw]

    @property
    def difficulty(self) -> int:
        try:
            value = int(self.meta.get("difficulty", 3))
        except (TypeError, ValueError):
            value = 3
        return min(5, max(1, value))

    @property
    def stem_with_hint(self) -> str:
        return self.stem

    @property
    def explanation(self) -> str:
        return self.sections.get("解析", "")

    @property
    def reference(self) -> str:
        return self.sections.get("参考答案", "")

    @property
    def hint(self) -> str:
        return self.sections.get("提示", "")

    @property
    def rubric(self) -> list[str]:
        """评分要点：每行一条，去掉列表符号。"""
        return _parse_rubric(self.sections.get("评分要点", ""))


# ---------------------------------------------------------------- front-matter


def split_front_matter(path: Path, text: str) -> tuple[dict[str, Any], int]:
    """切出 YAML front-matter，返回 (meta, 正文起始行号从 1 计数)。"""
    lines = text.splitlines()
    if not lines or not _FRONT_MATTER_FENCE.match(lines[0]):
        return {}, 1

    end = None
    for index in range(1, len(lines)):
        if _FRONT_MATTER_FENCE.match(lines[index]):
            end = index
            break
    if end is None:
        raise QuestionParseError(path, 1, "front-matter 没有闭合的 --- 行")

    raw = "\n".join(lines[1:end])
    try:
        import yaml  # type: ignore
    except ImportError as exc:  # pragma: no cover - 本机已装 PyYAML
        raise QuestionParseError(path, 1, f"需要 PyYAML 才能解析 front-matter：{exc}") from exc

    try:
        loaded = yaml.safe_load(raw) if raw.strip() else {}
    except Exception as exc:  # noqa: BLE001 - 交给调用方统一报错
        raise QuestionParseError(path, 2, f"front-matter YAML 解析失败：{exc}") from exc

    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise QuestionParseError(path, 2, "front-matter 必须是键值对映射")

    return dict(loaded), end + 2  # 正文从闭合行的下一行开始


# ---------------------------------------------------------------- 正文切分


def _split_sections(path: Path, body_lines: list[str], body_start: int) -> tuple[str, dict[str, str]]:
    """把正文切成 (题面, {小节名: 内容})，跳过围栏代码块内的标题。"""
    stem_lines: list[str] = []
    sections: dict[str, str] = {}
    current: str | None = None
    buffer: list[str] = []
    in_fence = False

    def flush() -> None:
        nonlocal buffer
        if current is not None:
            sections[current] = "\n".join(buffer).strip("\n")
        buffer = []

    for offset, line in enumerate(body_lines):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            (buffer if current is not None else stem_lines).append(line)
            continue

        match = None if in_fence else _SECTION_RE.match(line)
        if match:
            name = match.group(1).strip()
            flush()
            if name in SECTION_ORDER:
                current = name
                sections.setdefault(name, "")
            else:
                # 未知小节：视为题面的一部分，保持内容不丢失
                line_no = body_start + offset
                raise QuestionParseError(
                    path,
                    line_no,
                    f"未知小节「## {name}」，允许的小节为：{'、'.join(SECTION_ORDER)}",
                )
            continue

        (buffer if current is not None else stem_lines).append(line)

    flush()
    return "\n".join(stem_lines).strip("\n"), sections


_RUBRIC_BULLET_RE = re.compile(r"^(?:[-*+]|\d+[.、)])\s+")


def _parse_rubric(raw: str) -> list[str]:
    items: list[str] = []
    for line in raw.splitlines():
        text = line.strip()
        if not text:
            continue
        text = _RUBRIC_BULLET_RE.sub("", text)
        if text:
            items.append(text)
    return items


def _parse_parts(path: Path, raw: str, start_line: int) -> list[ProblemPart]:
    """把「## 小问」小节切成若干小问。

    每问以 `### 标题` 开始，问内可以用 `#### 参考答案` / `#### 评分要点` /
    `#### 提示` 三个四级标题进一步分块。
    """
    parts: list[ProblemPart] = []
    buffers: dict[int, dict[str, list[str]]] = {}
    current: int | None = None
    kind = "stem"
    in_fence = False

    def open_part(title: str, line_no: int) -> None:
        nonlocal current, kind
        parts.append(ProblemPart(index=len(parts) + 1, title=title))
        current = len(parts) - 1
        buffers[current] = {"stem": [], "reference": [], "rubric": [], "hint": []}
        kind = "stem"

    for offset, line in enumerate(raw.splitlines()):
        line_no = start_line + offset
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            if current is not None:
                buffers[current][kind].append(line)
            continue

        if not in_fence:
            head = PART_HEADING_RE.match(line)
            if head:
                open_part(head.group(1).strip(), line_no)
                continue

            sub = PART_SUB_RE.match(line)
            if sub:
                if current is None:
                    raise QuestionParseError(path, line_no, "小问内的四级标题必须写在某个 ### 小问之后")
                name = sub.group(1).strip()
                matched = next((k for prefix, k in _PART_SUB_KINDS if name.startswith(prefix)), None)
                if matched is None:
                    allowed = "、".join(f"#### {p}答案" for p, _ in _PART_SUB_KINDS)
                    raise QuestionParseError(
                        path, line_no, f"未知的小问小节「#### {name}」，允许：{allowed}"
                    )
                kind = matched
                continue

        if current is None:
            raise QuestionParseError(path, line_no, "「## 小问」小节里必须先写 `### <小问标题>`")
        buffers[current][kind].append(line)

    for part in parts:
        buf = buffers[part.index - 1]
        part.stem = "\n".join(buf["stem"]).strip("\n")
        part.reference = "\n".join(buf["reference"]).strip("\n")
        part.hint = "\n".join(buf["hint"]).strip("\n")
        part.rubric = _parse_rubric("\n".join(buf["rubric"]))

    return parts


def _parse_options(path: Path, raw: str, start_line: int) -> list[Option]:
    """解析选项列表。支持 `- A. xxx` / `- A、xxx` / `- xxx`（自动编号）。"""
    options: list[Option] = []
    auto_keys = "ABCDEFGHIJ"
    pending: Option | None = None

    for offset, line in enumerate(raw.splitlines()):
        if not line.strip():
            pending = None
            continue
        match = _OPTION_RE.match(line)
        if not match:
            if pending is not None and line.startswith((" ", "\t")):
                pending.text = f"{pending.text}\n{line.strip()}"
                continue
            raise QuestionParseError(path, start_line + offset, f"选项行无法解析：{line!r}")

        key, text = match.group(1), match.group(2).strip()
        if key is None:
            if len(options) >= len(auto_keys):
                raise QuestionParseError(path, start_line + offset, "选项超过 10 个，请显式指定字母")
            key = auto_keys[len(options)]
        pending = Option(key=key.upper(), text=text)
        options.append(pending)

    keys = [o.key for o in options]
    if len(set(keys)) != len(keys):
        dup = next(k for k in keys if keys.count(k) > 1)
        raise QuestionParseError(path, start_line, f"选项字母重复：{dup}")
    return options


def _normalize_answer(question: Question, raw: str) -> None:
    """按题型把「## 答案」小节规范化到 question.answer。"""
    qtype = question.type
    text = raw.strip()
    path = question.path

    if qtype in ("single", "multi"):
        tokens = [t.upper() for t in re.findall(r"[A-Za-z]", text)]
        # 去掉可能混入的“答案：”之类中文前缀残留
        if not tokens:
            raise QuestionParseError(path, 1, f"{qtype} 题的「## 答案」没有解析出任何选项字母")
        if qtype == "single":
            if len(set(tokens)) != 1:
                raise QuestionParseError(path, 1, f"single 题只能有 1 个答案，实际得到 {tokens}")
            question.answer = tokens[0]
        else:
            deduped = sorted(set(tokens))
            if len(deduped) < 2:
                raise QuestionParseError(path, 1, f"multi 题至少要有 2 个正确选项，实际得到 {deduped}")
            question.answer = deduped
        return

    if qtype == "blank":
        if not text:
            raise QuestionParseError(path, 1, "blank 题的「## 答案」不能为空")
        answers: list[list[str]] = []
        regexes: list[list[str]] = []
        for index, line in enumerate(text.splitlines()):
            line = line.strip()
            if not line:
                continue
            line = re.sub(r"^\d+[.、)]\s*", "", line)  # 去掉 “1. ” 编号

            # 约定：`~` 之后直到行尾整体是一条正则（正则内部可以自由使用 | 分支），
            # `~` 之前的部分才是用 | 分隔的字面可接受答案。这样避免了
            # 「分隔符 | 与正则分支 | 撞车」的问题。
            pattern: list[str] = []
            accept_part = line
            tilde = line.find(REGEX_PREFIX)
            if tilde != -1:
                regex_body = line[tilde + len(REGEX_PREFIX) :].strip()
                accept_part = line[:tilde]
                if regex_body:
                    pattern.append(regex_body)
            accept = [part.strip() for part in accept_part.split("|") if part.strip()]

            if not accept and not pattern:
                raise QuestionParseError(path, 1, f"blank 题第 {index + 1} 个空的答案为空")
            answers.append(accept)
            regexes.append(pattern)
        if not answers:
            raise QuestionParseError(path, 1, "blank 题的「## 答案」不能为空")
        question.answer = answers
        question.answer_regex = regexes
        return

    if qtype == "short":
        question.answer = None
        return

    raise QuestionParseError(path, 1, f"未知题型：{qtype!r}")


# ---------------------------------------------------------------- 主入口


def parse_question(path: Path) -> Question:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise QuestionParseError(path, 1, f"读取失败：{exc}") from exc

    meta, body_start = split_front_matter(path, text)
    body_lines = text.splitlines()[body_start - 1 :]
    stem, sections = _split_sections(path, body_lines, body_start)

    question = Question(path=path, meta=meta, stem=stem, sections=sections)

    if question.type not in VALID_TYPES:
        raise QuestionParseError(
            path,
            1,
            f"type 必须是 {'/'.join(VALID_TYPES)} 之一，实际为 {question.type!r}",
        )

    if "选项" in sections:
        option_line = _find_section_line(text, "选项")
        question.options = _parse_options(path, sections["选项"], option_line)

    if question.type == "problem":
        if "小问" not in sections:
            raise QuestionParseError(path, 1, "problem 题缺少「## 小问」小节")
        question.parts = _parse_parts(path, sections["小问"], _find_section_line(text, "小问"))
        if not question.parts:
            raise QuestionParseError(path, 1, "problem 题至少要有一个 `### <小问标题>`")
    elif "答案" in sections:
        _normalize_answer(question, sections["答案"])
    elif question.type != "short":
        raise QuestionParseError(path, 1, f"{question.type} 题缺少「## 答案」小节")

    return question


def _find_section_line(text: str, name: str) -> int:
    """定位某小节标题在原文中的行号（1 起），找不到返回 1。"""
    in_fence = False
    for index, line in enumerate(text.splitlines(), start=1):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _SECTION_RE.match(line)
        if match and match.group(1).strip() == name:
            return index + 1
    return 1


def iter_question_files(questions_dir: Path) -> list[Path]:
    """列出所有题目文件，排除模板与下划线开头的文件。"""
    files = [
        p
        for p in sorted(questions_dir.rglob("*.md"))
        if not p.name.startswith("_") and p.is_file()
    ]
    return files


ROOT = Path(__file__).resolve().parent.parent


def question_to_dict(question: Question) -> dict:
    """转成前端消费的结构。答案按题型规范化成统一形状。

    这是题目的**权威 JSON 表示**：离线构建（tools/build.py）与在线
    导入（api/）都调用这里。放在解析器里而不是各自的调用方，是为了
    避免两条路径各写一份序列化、然后悄悄漂移 —— 一旦漂移，
    在线版与离线版渲染出来的题目就会不一样。
    """
    if question.type == "single":
        answer: object = question.answer
    elif question.type == "multi":
        answer = list(question.answer or [])
    elif question.type == "blank":
        groups = question.answer or []
        regex_groups = question.answer_regex or []
        answer = [
            {
                "accept": list(group),
                "regex": list(regex_groups[i]) if i < len(regex_groups) else [],
            }
            for i, group in enumerate(groups)
        ]
    else:
        answer = None

    try:
        rel = question.path.relative_to(ROOT)
    except ValueError:
        rel = question.path

    return {
        "id": question.id,
        "type": question.type,
        "topic": question.topic,
        "chapter": str(question.meta.get("chapter", "") or ""),
        "tags": question.tags,
        "difficulty": question.difficulty,
        # 掌握层次与训练类型。界面上不显示，但必须随题走：
        # 判断「某个知识点已经覆盖了哪几层、哪几翼」只能靠它们，
        # 而这件事每次出题都要做（新开 session 时这是唯一的记忆）。
        "layer": str(question.meta.get("layer", "") or ""),
        "wing": str(question.meta.get("wing", "") or ""),
        "source": str(question.meta.get("source", "") or ""),
        "stem": question.stem,
        "hint": question.hint,
        "options": [{"key": o.key, "text": o.text} for o in question.options],
        "answer": answer,
        # blank 题的作答形态：inline（单行输入）| code（多行代码输入）
        "blankMode": str(question.meta.get("blankMode", "") or ""),
        "blankLines": int(question.meta.get("blankLines", 0) or 0),
        "explanation": question.explanation,
        "reference": question.reference,
        "rubric": question.rubric,
        "parts": [
            {
                "index": p.index,
                "title": p.title,
                "stem": p.stem,
                "reference": p.reference,
                "rubric": p.rubric,
                "hint": p.hint,
            }
            for p in question.parts
        ],
        "file": str(rel),
    }


if __name__ == "__main__":  # 手工调试用
    import sys as _sys

    if len(_sys.argv) < 2:
        print("用法: python3 tools/question_parser.py <题目文件.md>")
        raise SystemExit(2)
    parsed = parse_question(Path(_sys.argv[1]))
    print(f"id={parsed.id} type={parsed.type} topic={parsed.topic} tags={parsed.tags}")
    print(f"options={[o.key for o in parsed.options]}")
    print(f"answer={parsed.answer!r}")
