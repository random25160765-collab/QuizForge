"""笔记库核心 —— **文件是权威**，这里只做读写与解析。

权威边界（B 段定下的）：题库 / 掌握度 / 作答记录在数据库；笔记 / 资料 / 索引在**文件**里，
落在应用数据目录（开发时是仓库的 `data/`，打包后是 `~/quizforge[-beta]/`）。

所以这个模块里没有 ORM、没有表：一份 Markdown 就是一份笔记，YAML 头承载标题与标签，
`[[双链]]` 承载关系；索引是派生物，删了能重建（A2 做）。

只放**两边都要用**的东西：导入器（`tools/import_vault.py`）解析源库、后端（A2 的
`routers/notes.py`）读同一批文件 —— 解析规则不能有两份，否则"导入时算出来的反链"
与"界面上看到的反链"迟早对不上，而且是对不上了才发现。
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

#: 元数据头的围栏。Obsidian 与仓库里的题库文件用的是同一个约定（`---` 各占一行）
FENCE = "---"

#: 导入器补的头会带上这个键 = True —— 让"哪一行是我补的"永远可识别、可批量回退。
#: 刻意不写成 `origin: machine` 那种"看起来像作者字段"的名字：它会混进真正的元数据里。
GENERATED_KEY = "qf_generated"

#: 一个头最多允许多少行。超过就不再找了 —— 多半那只是一条正文里的分隔线
MAX_HEAD_LINES = 60

#: 笔记文件的两类：`.md` 是正文，`.canvas` 是画布（一等条目，见 A3）
NOTE_SUFFIXES = (".md", ".canvas")
MD_SUFFIX = ".md"
CANVAS_SUFFIX = ".canvas"

_WIKILINK = re.compile(r"(?P<embed>!)?\[\[(?P<inner>[^\[\]]+)\]\]")
_MDLINK = re.compile(r"(?P<embed>!)?\[(?P<text>[^\[\]]*)\]\((?P<href>[^()\s]+)(?:\s+\"[^\"]*\")?\)")
_FENCE_LINE = re.compile(r"^\s*(?P<fence>```+|~~~+)")
_INLINE_CODE = re.compile(r"`[^`]*`")
_H1 = re.compile(r"^\s*#\s+(?P<title>.+?)\s*$")


# ------------------------------------------------------------------ 结构


@dataclass(frozen=True)
class Link:
    """一处引用（双链或标准 Markdown 链接）。"""

    target: str                 # 去掉锚点与别名之后的指向
    alias: str = ""
    anchor: str = ""            # `#page=19&rect=…` 这类
    embed: bool = False         # `![[…]]` / `![…]()` —— 是嵌入（图片、PDF 某一页）
    raw: str = ""

    @property
    def is_file(self) -> bool:
        """指向一个**文件**（带后缀）而不是一篇笔记。区分它才知道该不该当附件搬。"""
        return bool(Path(self.target).suffix)


@dataclass
class Note:
    """一份解析过的笔记（只是视图，不改文件）。"""

    meta: dict[str, Any] = field(default_factory=dict)
    body: str = ""
    had_header: bool = False
    broken_header: bool = False

    @property
    def generated(self) -> bool:
        """这个头是导入器补的吗 —— 决定"能不能安全地整段重写"。"""
        return bool(self.meta.get(GENERATED_KEY))


@dataclass(frozen=True)
class Resolved:
    """一处引用解析到库里哪个文件。"""

    rel: str | None = None
    ambiguous: bool = False     # 同名多个，取的是排序第一个 —— 值得在清单里提一句

    def __bool__(self) -> bool:
        return self.rel is not None


# ------------------------------------------------------------------ 解析


def split_text(text: str) -> Note:
    """切出元数据头与正文。

    **对坏头不猜、也不修**：第 1 行是 `---` 却没闭合时，如实标 `broken_header` —— 那种文件
    如果我"贴心地"再补一个头，就会变成两个头叠在一起，比原样留着更糟。
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != FENCE:
        return Note(body=text)

    end = None
    for index in range(1, min(len(lines), MAX_HEAD_LINES)):
        if lines[index].strip() == FENCE:
            end = index
            break
    if end is None:
        return Note(body=text, broken_header=True)

    try:
        loaded = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError:
        return Note(body=text, broken_header=True)
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        return Note(body=text, broken_header=True)

    body = "\n".join(lines[end + 1 :])
    if body:
        body = body.lstrip("\n")
    if text.endswith("\n") and not body.endswith("\n"):
        body += "\n"
    return Note(meta=dict(loaded), body=body, had_header=True)


def read_note(path: Path) -> Note:
    return split_text(read_text(path))


def read_text(path: Path) -> str:
    """按 UTF-8 读；读不动就**替换着读**（原样保住其余内容，别让一篇坏编码挡住整次导入）。"""
    data = path.read_bytes()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("utf-8", "replace")


def render(meta: Mapping[str, Any], body: str) -> str:
    """把头与正文拼回一份文件。"""
    head = yaml.safe_dump(dict(meta), allow_unicode=True, sort_keys=False).rstrip("\n")
    text = f"{FENCE}\n{head}\n{FENCE}\n\n{body.lstrip(chr(10))}"
    return text if text.endswith("\n") else text + "\n"


def minimal_meta(body: str, stem: str) -> dict[str, Any]:
    """给没有元数据头的笔记补一个**最小**头。

    只写三样，且都能一眼看出是我补的：

    * `title` —— 正文第一个 `#` 标题，没有就用文件名（笔记名本来就是文件名）；
    * `tags` —— 空列表。**不猜标签**：猜出来的标签会混进真正的分类里，而 A2 有
      "让模型建议标签、逐条接受"的入口，那才是猜该待的地方；
    * `qf_generated: true` —— 标记这个头不是作者写的。
    """
    for line in body.splitlines()[:MAX_HEAD_LINES]:
        if match := _H1.match(line):
            title = match.group("title").strip()
            if title:
                return {"title": title, "tags": [], GENERATED_KEY: True}
    return {"title": stem, "tags": [], GENERATED_KEY: True}


# ------------------------------------------------------------------ 引用


def _prose_lines(text: str) -> Iterator[str]:
    """只吐**正文行**：围栏代码块里的不算。

    代码里写着 `[[x]]` 不该变成一条反链 —— 与题库解析器同一个讲究（那边是"跳过围栏里的
    标题"）。同理顺手抹掉行内代码段，避免文档里举例子也变成真引用。
    """
    fence: str | None = None
    for line in text.splitlines():
        match = _FENCE_LINE.match(line)
        if match:
            marker = match.group("fence")[0] * 3
            if fence is None:
                fence = marker
            elif line.strip().startswith(fence):
                fence = None
            continue
        if fence is None:
            yield _INLINE_CODE.sub("", line)


def iter_links(text: str) -> Iterator[Link]:
    """正文里所有引用（双链在前、Markdown 链接在后，各自保持出现顺序）。"""
    for line in _prose_lines(text):
        for match in _WIKILINK.finditer(line):
            target, _, alias = match.group("inner").partition("|")
            target, _, anchor = target.partition("#")
            yield Link(
                target=target.strip(),
                alias=alias.strip(),
                anchor=anchor.strip(),
                embed=bool(match.group("embed")),
                raw=match.group(0),
            )
        for match in _MDLINK.finditer(line):
            href = match.group("href").strip()
            if href.startswith(("http://", "https://", "mailto:", "data:", "#")):
                continue
            target, _, anchor = href.partition("#")
            yield Link(
                target=target.strip(),
                alias=match.group("text").strip(),
                anchor=anchor.strip(),
                embed=bool(match.group("embed")),
                raw=match.group(0),
            )


def build_index(rels: Iterable[str]) -> dict[str, list[str]]:
    """按**文件名**给库里的相对路径建索引。

    为什么不是按完整路径：双链通常只写文件名（`[[卷积]]`），而文件在深层目录里。
    Obsidian 自己也是这么找的 —— 同名多个时它的规则最复杂，我们取排序第一个并标
    `ambiguous`，在清单里说出来，不假装没有歧义。
    """
    index: dict[str, list[str]] = defaultdict(list)
    for rel in rels:
        index[Path(rel).name].append(rel)
    return {name: sorted(paths) for name, paths in index.items()}


def resolve(
    target: str,
    *,
    by_name: Mapping[str, list[str]],
    rels: set[str],
    current_dir: str = "",
) -> Resolved:
    """把一处引用解析成库里的相对路径（先按路径、再按文件名）。"""
    if not target:
        return Resolved()
    cleaned = target.strip().lstrip("./").replace("\\", "/")
    if current_dir in ("", "."):
        candidates = [cleaned]
    else:
        candidates = [f"{current_dir}/{cleaned}", cleaned]
    if not Path(cleaned).suffix:
        candidates = [*candidates, *[f"{item}.md" for item in candidates]]
    for candidate in candidates:
        if candidate in rels:
            return Resolved(candidate)

    hits = list(by_name.get(Path(cleaned).name) or [])
    if not hits and not Path(cleaned).suffix:
        hits = list(by_name.get(f"{Path(cleaned).name}.md") or [])
    if hits:
        return Resolved(hits[0], ambiguous=len(hits) > 1)
    return Resolved()


def note_id(vault: str, rel: str) -> str:
    """一篇笔记的**稳定引用键**：`<库名>/<库内相对路径>`。

    不带机器相关的绝对路径 —— 源目录换台机器就没了，而引用键要能长期对上
    （资料模块的引用键是同一个讲究，见 A4）。
    """
    return f"{vault}/{Path(rel).as_posix()}"


# ------------------------------------------------------------------ 大纲

#: 缩进单位。库里用制表符就按制表符算、用空格就按检测出来的那个数算（见 `detect_indent_unit`）
DEFAULT_INDENT = "\t"
TAB_STOP = 4

_HEADING = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<text>.*)$")
_BULLET = re.compile(r"^(?P<indent>[ \t]*)(?P<marker>[-*+]|\d+[.)])(?P<space>\s+)(?P<text>.*)$")
_QUOTE = re.compile(r"^\s*>")
_HRULE = re.compile(r"^\s*([-*_])\1{2,}\s*$")
_MATH = re.compile(r"^\s*\$\$")


@dataclass
class OutlineLine:
    """大纲里的一行（幕布那边叫一个"节点"）。

    组件返回的是**扁平表**（每行带 `level`），不是嵌套结构。原因是编辑是按行的：
    插一行、改一行、删一行、把这一行连同子树挪走 —— 扁平表让"行号"始终是那把唯一的尺子；
    嵌套结构一改就得整体重算，行号也跟着漂，然后你按错了行。
    """

    index: int
    level: int
    kind: str           # heading | bullet | text | quote | math | blank | code | rule
    raw: str
    text: str           # 去掉标记之后的内容（展示用；写回一律用 raw）
    foldable: bool = False   # 紧跟其后有更深的行（可以折叠）


def _column(indent: str) -> int:
    """把缩进折算成列号（制表符按 4 的倍数展开，与编辑器的制表位一致）。"""
    column = 0
    for char in indent:
        column = column + TAB_STOP - column % TAB_STOP if char == "\t" else column + 1
    return column


def outline(body: str) -> list[OutlineLine]:
    """把正文切成大纲行。

    幕布那半边的地基：**每一行都是一个节点**。层级怎么算：

    * 标题（`#`…`######`）自带层级，并把它后面的一组整体压到下一层 —— 于是
      "先分节、节里再列条目"这种最常见的写法天然成树；
    * 项目符号的层级**不看绝对缩进、只看相对位置**（一个缩进列栈）：用制表符、
      用两个空格、用四个空格的库都能算对 —— 实测那个库用的是制表符，
      但没理由把别人的两个空格当成同级；
    * 空行跟着它**下面**那一行的层级（否则折叠时会在空行处断开，看起来像 bug）；
    * 代码块里的行不参与解析（`kind="code"`）—— 与 `iter_links` 同一个讲究。
    """
    lines, _ = split_lines(body)
    out: list[OutlineLine] = []
    heading_level = -1                      # 最近一个标题的层级（-1 = 还没遇到标题）
    stack: list[tuple[int, int]] = []       # 打开着的路径：(缩进列, 层级)
    fence: str | None = None
    math_open = False                       # 是否在 `$$ … $$` 块里

    def level_for(indent: str) -> int:
        """按缩进算层级 —— 大纲算法里最经典的那套。

        退回到已有某一列 = 同级；比它更深 = 下一级；栈空了就从**本次的基准**
        （最近的标题 + 1）起。只看**相对**位置，于是制表符、两个空格、四个空格的
        库都能算对。

        符号与普通文字一视同仁：实测那个库里的 MENU 笔记就是"文字 + 制表符顶出条目"，
        只认项目符号的话整篇 MENU 会摊平成一层，幕布那半边就白做了。
        """
        column = _column(indent)
        while stack and column < stack[-1][0]:
            stack.pop()
        if stack and column == stack[-1][0]:
            return stack[-1][1]
        level = stack[-1][1] + 1 if stack else heading_level + 1
        stack.append((column, level))
        return level

    for index, raw in enumerate(lines):
        marker = _FENCE_LINE.match(raw)
        if fence is not None:
            if raw.strip().startswith(fence):
                fence = None
            current = stack[-1][1] if stack else heading_level + 1
            out.append(OutlineLine(index, current, "code", raw, raw))
            continue
        if marker:
            fence = marker.group("fence")[0] * 3
            current = stack[-1][1] if stack else heading_level + 1
            out.append(OutlineLine(index, current, "code", raw, raw))
            continue
        if not raw.strip():
            out.append(OutlineLine(index, 0, "blank", raw, ""))
            continue
        # 公式块（`$$ … $$`）与代码块同样是"哑内容"，**不参与层级**。
        # 数学笔记里的多行公式常靠缩进排版（实测一篇里 138 行是这么来的），
        # 拿它当父子关系会把大纲撑到三四层，整页就没法看了。
        current = stack[-1][1] if stack else heading_level + 1
        if math_open:
            if "$$" in raw:
                math_open = False
            out.append(OutlineLine(index, current, "math", raw, raw))
            continue
        if _MATH.match(raw):
            if raw.count("$$") == 1:
                math_open = True
            out.append(OutlineLine(index, current, "math", raw, raw.strip()))
            continue
        if match := _HEADING.match(raw):
            heading_level = len(match.group("hashes")) - 1
            # 标题另起一节：路径清空，于是这一节里的第一层从 `标题层级 + 1` 开始
            stack.clear()
            out.append(OutlineLine(index, heading_level, "heading", raw, match.group("text").strip()))
            continue
        if match := _BULLET.match(raw):
            out.append(
                OutlineLine(
                    index,
                    level_for(match.group("indent")),
                    "bullet",
                    raw,
                    match.group("text").strip(),
                )
            )
            continue
        if _HRULE.match(raw):
            current = stack[-1][1] if stack else heading_level + 1
            out.append(OutlineLine(index, current, "rule", raw, ""))
            continue
        indent = raw[: len(raw) - len(raw.lstrip(" \t"))]
        level = level_for(indent)
        if _QUOTE.match(raw):
            out.append(OutlineLine(index, level, "quote", raw, raw.strip()))
        else:
            out.append(OutlineLine(index, level, "text", raw, raw.strip()))

    # 空行跟着下面那一行：先倒着扫一遍补层级，再定"能不能折叠"
    for pos in range(len(out) - 1, -1, -1):
        if out[pos].kind == "blank":
            if pos + 1 < len(out):
                out[pos].level = out[pos + 1].level
            elif pos > 0:
                out[pos].level = out[pos - 1].level
    for pos, item in enumerate(out):
        item.foldable = pos + 1 < len(out) and out[pos + 1].level > item.level
    return out


def detect_indent_unit(body: str) -> str:
    """这个库（这份笔记）用制表符还是空格缩进 —— 缩进操作要跟着它，别混用两种。"""
    tabs = spaces = 0
    for raw in split_lines(body)[0]:
        match = _BULLET.match(raw)
        if not match:
            continue
        if "\t" in match.group("indent"):
            tabs += 1
        elif match.group("indent"):
            spaces += 1
    return DEFAULT_INDENT if tabs >= spaces else "  "


def split_lines(body: str) -> tuple[list[str], bool]:
    """切成行，并记住**是否以换行结尾**。

    写回时照原样恢复：凭空多一个或少一个结尾换行，会让"每编辑一次文件尾部就多一行"，
    或者更糟 —— 让最后一次改动看起来像改了两行（diff 里全是噪音）。
    """
    if body == "":
        return [], False
    trailing = body.endswith("\n")
    text = body[:-1] if trailing else body
    return text.split("\n"), trailing


def join_lines(lines: list[str], trailing: bool) -> str:
    text = "\n".join(lines)
    return text + "\n" if trailing and text != "" else text


# ------------------------------------------------------------------ 行级操作
#
# 幕布那半边的核心：一切编辑都是**对某一行的操作**。全部是纯函数（进正文、出正文），
# 不碰文件 —— 写入与快照在 `notelib` 里，出错能整体回滚。


def line_at(body: str, index: int) -> str:
    lines, _ = split_lines(body)
    if 0 <= index < len(lines):
        return lines[index]
    raise IndexError(f"第 {index} 行不存在（共 {len(lines)} 行）")


def insert_line(body: str, after: int, raw: str) -> str:
    """在第 `after` 行**之后**插一行（`after = -1` 插到最前）。

    幕布里回车是"新建同级节点"，这件事由调用方决定插什么（`notelib` 会照着目标行的
    标记与缩进生成一个空条目），这里只负责插对位置。
    """
    lines, trailing = split_lines(body)
    position = max(0, min(after + 1, len(lines)))
    lines.insert(position, raw)
    return join_lines(lines, trailing or body == "")


def replace_line(body: str, index: int, raw: str) -> str:
    lines, trailing = split_lines(body)
    if not 0 <= index < len(lines):
        raise IndexError(f"第 {index} 行不存在（共 {len(lines)} 行）")
    lines[index] = raw
    return join_lines(lines, trailing)


def delete_line(body: str, index: int) -> str:
    lines, trailing = split_lines(body)
    if not 0 <= index < len(lines):
        raise IndexError(f"第 {index} 行不存在（共 {len(lines)} 行）")
    lines.pop(index)
    return join_lines(lines, trailing)


def shift_line(body: str, index: int, delta: int, unit: str = DEFAULT_INDENT) -> str:
    """缩进 / 反缩进一行（幕布的 Tab / Shift+Tab）。

    `delta > 0` 加一级；`delta < 0` 减一级（减到没有为止，不会吃掉正文）。
    """
    lines, trailing = split_lines(body)
    if not 0 <= index < len(lines):
        raise IndexError(f"第 {index} 行不存在（共 {len(lines)} 行）")
    raw = lines[index]
    indent_len = len(raw) - len(raw.lstrip(" \t"))
    indent, rest = raw[:indent_len], raw[indent_len:]
    if delta > 0:
        indent += unit * delta
    else:
        for _ in range(-delta):
            if indent.startswith(unit):
                indent = indent[len(unit) :]
            elif indent.startswith("\t"):
                indent = indent[1:]
            elif indent.startswith(" "):
                # 空格缩进的库：退一级 —— 不足一级就把这段空格去光（别退到负数）
                stripped = indent.lstrip(" ")
                indent = (
                    stripped
                    if len(indent) - len(stripped) <= len(unit)
                    else " " * (len(indent) - len(unit))
                )
            else:
                break
    lines[index] = indent + rest
    return join_lines(lines, trailing)


def block_extent(body: str, index: int) -> tuple[int, int]:
    """一行的"子树"范围（含自己）：后面紧跟的、缩进更深的行，以及其中的空行。

    空行算在块里 —— 否则每次挪动都会把段落间的空行留在原地，越挪越乱。
    """
    lines, _ = split_lines(body)
    if not 0 <= index < len(lines):
        raise IndexError(f"第 {index} 行不存在（共 {len(lines)} 行）")
    base = len(lines[index]) - len(lines[index].lstrip(" \t"))
    end = index + 1
    while end < len(lines):
        raw = lines[end]
        if not raw.strip():
            end += 1
            continue
        if len(raw) - len(raw.lstrip(" \t")) > base:
            end += 1
            continue
        break
    # 尾部的空行不跟着走（它们属于下一块）
    while end - 1 > index and not lines[end - 1].strip():
        end -= 1
    return index, end


def move_block(body: str, index: int, to: int) -> str:
    """把第 `index` 行的整棵子树挪到第 `to` 行**之前**（幕布的 Alt+↑/↓）。"""
    lines, trailing = split_lines(body)
    start, end = block_extent(body, index)
    # 只要不是**挪进自己内部**就都合法：`to == start` / `to == end` 是原地不动（无害），
    # 挪到紧跟自己后面（`to == end`）就是"放到下一块之前"，是最常用的那个动作。
    # 早先写成 `to in range(start, end + 1)` 把这三种全拦了 —— 实测第一个用例就撞上。
    if start < to < end:
        raise ValueError("不能把一块挪进它自己里面")
    block = lines[start:end]
    rest = lines[:start] + lines[end:]
    # `to` 是"按**移动前**的行号，插到第 to 行之前"。块被抽走之后，它后面的行号要往前挪
    # `end - start` —— 只对 `to < end` 不调整。写成 `to > end` 就差一格：
    # `to == end`（= 原地不动）会被算成往后挪过一整块（实测就是这条用例挂的）。
    target = to - (end - start) if to >= end else to
    target = max(0, min(target, len(rest)))
    return join_lines(rest[:target] + block + rest[target:], trailing)


# ------------------------------------------------------------------ 改名


def _mask_inline_code(line: str) -> str:
    """把行内代码段换成**等长的空格**。

    这样匹配到的下标仍然能直接用在原行上（长度没变），而"代码里举的例子"不会被误改 ——
    比自己重算一遍索引简单，也不会算错。
    """
    return _INLINE_CODE.sub(lambda match: " " * len(match.group(0)), line)


def rewrite_links(text: str, replace_target: Callable[[str], str | None]) -> tuple[str, int]:
    """把引用改成新目标，**保留别名与锚点**（`[[目标#锚点|别名]]`）。

    只动**正文行**：围栏代码块整段跳过、行内代码段先掩掉 —— 与 `iter_links` 同一套判据。
    改名时最怕的就是把"文档里举的例子"也改了（实测第一版就是这么错的：三处全改了）。

    `replace_target` 收到解析出来的目标，返回新目标（`None` = 这条不动）。
    """
    out: list[str] = []
    changed = 0
    fence: str | None = None
    for line in text.splitlines(keepends=True):
        marker = _FENCE_LINE.match(line)
        if fence is not None:
            out.append(line)
            if line.strip().startswith(fence):
                fence = None
            continue
        if marker:
            fence = marker.group("fence")[0] * 3
            out.append(line)
            continue

        masked = _mask_inline_code(line)
        rebuilt = line
        # 倒着改：后面的替换不会让前面的下标失效
        for pattern in (_WIKILINK, _MDLINK):
            for match in reversed(list(pattern.finditer(masked))):
                if pattern is _WIKILINK:
                    target, alias_sep, alias = match.group("inner").partition("|")
                    target, anchor_sep, anchor = target.partition("#")
                    new = replace_target(target.strip())
                    if new is None:
                        continue
                    piece = (
                        f"{match.group('embed') or ''}"
                        f"[[{new}{anchor_sep}{anchor}{alias_sep}{alias}]]"
                    )
                else:
                    target, anchor_sep, anchor = match.group("href").partition("#")
                    new = replace_target(target.strip())
                    if new is None:
                        continue
                    piece = (
                        f"{match.group('embed') or ''}[{match.group('text')}]"
                        f"({new}{anchor_sep}{anchor})"
                    )
                rebuilt = rebuilt[: match.start()] + piece + rebuilt[match.end() :]
                changed += 1
        out.append(rebuilt)
    return "".join(out), changed

# ------------------------------------------------------------------ 正文写回


def head_block(text: str) -> str:
    """原文里元数据头那一段（含两行 `---`）。没有头就是空串。"""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != FENCE:
        return ""
    for index in range(1, min(len(lines), MAX_HEAD_LINES)):
        if lines[index].strip() == FENCE:
            return "".join(lines[: index + 1])
    return ""


def splice_body(text: str, body: str) -> str:
    """只换正文、**原样保留元数据头**。

    为什么不重新 dump 一遍 YAML：那会把作者写下的引号风格、注释、键序全改掉 ——
    明明只动了正文，diff 里整个头都变了。而且那个头往往不是我们写的（源笔记自带的）。
    """
    head = head_block(text).rstrip("\n")
    if not head:
        return body
    # head 自己带着结尾换行，再拼 `\n\n` 会多出一个空行 —— 每存一次文件就长一行空，
    # diff 里看得见（实测确实多了一个，这条注释就是为它写的）
    return f"{head}\n\n{body.lstrip(chr(10))}"


# ------------------------------------------------------------------ 行内标签


#: 正文里的 `#标签`（Obsidian 的写法）。
#:
#: 这条正则刻意收得很紧，因为**放宽会立刻误报**：实测在一个真实数学库里，
#: 宽松版本把 LaTeX 的 `#\{i|\dim` 和整句中文（"井号代表集合内元素的个数%%"）都当成了标签。
#: 于是：`#` 后必须紧跟字母 / 下划线 / 汉字（标题的 `# 空格` 天然出局），
#: 之后只允许字母数字、汉字、`-`、`/`、`.`，且总长有上限 —— 标签是短的。
_INLINE_TAG = re.compile(r"(?<![\w/#\\])#([A-Za-z_\u4e00-\u9fff][\w\u4e00-\u9fff/.\-]{0,63})")
#: 落在标签尾巴上的标点不算标签的一部分（`见 #微积分。` → `微积分`）
_TAG_TAIL = ".-/"


def iter_inline_tags(text: str) -> Iterator[str]:
    """正文里的行内标签（去重前的原样，按出现顺序）。

    只扫正文行：代码块里的 `#include` 之类不是标签 —— 与 `iter_links` 同一个讲究。
    """
    for line in _prose_lines(text):
        for found in _INLINE_TAG.findall(line):
            yield found.rstrip(_TAG_TAIL)


# ------------------------------------------------------------------ 指纹


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ------------------------------------------------------------------ 检索表达式
#
# 形状照搬 Trilium 的表达式检索（`packages/trilium-core/src/services/search/`）：
# `#标签` / `title:` / `-` 取反 / `AND` `OR` `not` / 括号 / `orderby` / `limit`。
# 它是**纯函数** —— 输入一批已解析好的条目，输出命中集合，与存储无关，所以能整套搬过来。
#
# 为什么不做成"搜索框 = 子串匹配"：库一大，"带某标签、标题含某词、而且还没整理"这种念头
# 会天天冒出来，子串匹配表达不了"且"与"非"。表达式是这套东西里唯一越用越值钱的部分。

#: 排序字段 → 取值函数。与 Trilium 的 `orderby` 对应的最小集合。
_ORDER_FIELDS = {
    "title": lambda row: row.get("title", ""),
    "path": lambda row: row.get("rel", ""),
    "created": lambda row: row.get("created", ""),
    "modified": lambda row: row.get("modified", 0.0),
    "size": lambda row: row.get("size", 0),
    "links": lambda row: row.get("links", 0),
}


@dataclass
class Query:
    """解析好的检索表达式。`match` 收一条"检索行"（见 `search_row`）。"""

    match: Callable[[dict], bool]
    order: str = ""
    desc: bool = False
    limit: int = 0
    text: str = ""

    def sort_key(self, row: dict) -> Any:
        field = _ORDER_FIELDS.get(self.order)
        return field(row) if field else 0


def _tokenize_query(text: str) -> list[str]:
    """切词。引号里的整段算一个词（用引号包着保留，便于后面识别"这是短语"）。"""
    tokens: list[str] = []
    buffer = ""
    quoted = False
    for char in text:
        if char == '"':
            quoted = not quoted
            if not quoted:
                tokens.append(f'"{buffer}"')
                buffer = ""
            continue
        if quoted:
            buffer += char
            continue
        if char in "()":
            if buffer:
                tokens.append(buffer)
                buffer = ""
            tokens.append(char)
            continue
        if char.isspace():
            if buffer:
                tokens.append(buffer)
                buffer = ""
            continue
        buffer += char
    if buffer:
        tokens.append(f'"{buffer}"' if quoted else buffer)
    return tokens


def _strip_quotes(token: str) -> str:
    return token[1:-1] if len(token) >= 2 and token.startswith('"') and token.endswith('"') else token


def _atom(token: str, row: dict) -> bool:
    """一个字段/词条是否命中。`token` 已经去掉取反前缀。"""
    raw = _strip_quotes(token)
    lowered = raw.lower()
    if lowered in ("and", "or", "not"):
        return True  # 运算符不该走到这里；真走到了就当空条件（不炸）

    if raw.startswith("#"):
        tag = raw[1:]
        if "=" in tag:                      # `#标签=值`：我们的标签是纯字符串，值只作别名
            tag = tag.split("=", 1)[1]
        wanted = tag.strip().lower()
        return any(wanted == str(item).strip().lower() for item in row.get("tags", []))

    if ":" in raw:
        field, _, value = raw.partition(":")
        key = field.strip().lower()
        want = value.strip().lower()
        if key == "title":
            return want in str(row.get("title", "")).lower()
        if key == "path":
            return want in str(row.get("rel", "")).lower()
        if key == "type":
            return want == str(row.get("kind", "note"))
        if key == "tag":
            return any(want == str(item).strip().lower() for item in row.get("tags", []))
        if key == "is":
            if want == "unresolved":
                return bool(row.get("unresolved", 0))
            if want == "empty":
                return not str(row.get("body", "")).strip()
            if want == "generated":
                return bool(row.get("generated", False))
            if want == "archived":
                return bool(row.get("archived", False))
            return False
        # 不认识的字段：当成普通词，别让一个笔误把结果清空
        lowered = raw.lower()

    if not lowered:
        return True
    return lowered in str(row.get("haystack", ""))


def parse_query(text: str) -> Query:
    """解析检索表达式。

    语法（`AND` 与相邻即与，`OR` 绑定更松）：

        term                 正文或标题包含
        "两个 词"             短语
        #标签 / #标签=值       有某个标签
        title:词 path:词 tag:词 type:note|canvas
        is:unresolved|empty|generated|archived
        -原子                取反
        A AND B / A OR B / ( … ) / not A
        orderby title|path|created|modified|size|links [asc|desc]
        limit 20
    """
    tokens = _tokenize_query(text or "")
    order = ""
    desc = False
    limit = 0
    kept: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index].lower()
        if token == "orderby" and index + 1 < len(tokens):
            order = _strip_quotes(tokens[index + 1]).lower()
            index += 2
            if index < len(tokens) and tokens[index].lower() in ("asc", "desc"):
                desc = tokens[index].lower() == "desc"
                index += 1
            continue
        if token == "limit" and index + 1 < len(tokens):
            try:
                limit = max(0, int(_strip_quotes(tokens[index + 1])))
            except ValueError:
                limit = 0
            index += 2
            continue
        kept.append(tokens[index])
        index += 1

    position = 0

    def peek() -> str:
        return kept[position].lower() if position < len(kept) else ""

    def parse_or() -> Callable[[dict], bool]:
        nonlocal position
        node = parse_and()
        while peek() == "or":
            position += 1
            right = parse_and()
            left = node
            node = lambda row, left=left, right=right: left(row) or right(row)
        return node

    def parse_and() -> Callable[[dict], bool]:
        nonlocal position
        node = parse_unary()
        while position < len(kept) and peek() not in ("or", ")"):
            if peek() == "and":
                position += 1
            if position >= len(kept) or peek() in ("or", ")"):
                break
            right = parse_unary()
            left = node
            node = lambda row, left=left, right=right: left(row) and right(row)
        return node

    def parse_unary() -> Callable[[dict], bool]:
        nonlocal position
        token = peek()
        if token in ("not", "-"):
            position += 1
            inner = parse_unary()
            return lambda row, inner=inner: not inner(row)
        return parse_primary()

    def parse_primary() -> Callable[[dict], bool]:
        nonlocal position
        if position >= len(kept):
            return lambda row: True
        token = kept[position]
        if token == "(":
            position += 1
            node = parse_or()
            if position < len(kept) and kept[position] == ")":
                position += 1
            return node
        if token == ")":
            position += 1
            return lambda row: True
        position += 1
        if token.startswith("-") and len(token) > 1:
            inner = token[1:]
            return lambda row, t=inner: not _atom(t, row)
        return lambda row, t=token: _atom(t, row)

    predicate = parse_or() if kept else (lambda row: True)
    return Query(match=predicate, order=order if order in _ORDER_FIELDS else "", desc=desc, limit=limit, text=text)


def search_row(rel: str, title: str, body: str, tags: list[str], **extra: Any) -> dict[str, Any]:
    """把一条笔记拍成"检索行"。表达式只认这一种形状，于是解析与存储解耦。"""
    row: dict[str, Any] = {
        "rel": rel,
        "title": title,
        "body": body,
        "tags": tags,
        "haystack": (title + "\n" + body).lower(),
        "kind": extra.pop("kind", "note"),
    }
    row.update(extra)
    return row


__all__ = [
    "CANVAS_SUFFIX",
    "DEFAULT_INDENT",
    "FENCE",
    "GENERATED_KEY",
    "MD_SUFFIX",
    "NOTE_SUFFIXES",
    "Link",
    "Note",
    "OutlineLine",
    "Resolved",
    "block_extent",
    "build_index",
    "delete_line",
    "detect_indent_unit",
    "head_block",
    "insert_line",
    "iter_inline_tags",
    "iter_links",
    "join_lines",
    "line_at",
    "minimal_meta",
    "move_block",
    "note_id",
    "outline",
    "read_note",
    "read_text",
    "render",
    "replace_line",
    "resolve",
    "rewrite_links",
    "search_row",
    "sha256_bytes",
    "sha256_file",
    "shift_line",
    "splice_body",
    "split_lines",
    "split_text",
]
