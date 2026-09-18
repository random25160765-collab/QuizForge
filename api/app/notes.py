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
from collections.abc import Iterable, Iterator, Mapping
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


# ------------------------------------------------------------------ 指纹


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "CANVAS_SUFFIX",
    "FENCE",
    "GENERATED_KEY",
    "MD_SUFFIX",
    "NOTE_SUFFIXES",
    "Link",
    "Note",
    "Resolved",
    "build_index",
    "iter_links",
    "minimal_meta",
    "note_id",
    "read_note",
    "read_text",
    "render",
    "resolve",
    "sha256_bytes",
    "sha256_file",
    "split_text",
]
