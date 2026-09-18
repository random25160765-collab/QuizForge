"""资料库：多根扫描、条目归拢、元数据推断、引用键、全文缓存。

**一个条目 = 一个主文件 + 它引用的附属资源**（图片等）。不做多文件合并 ——
成套文档要归到一起，用标签与目录表达，不新造"集合"这个概念。

三层权威，别混：

* `data/library/{citekey}.yaml` —— **元数据**（人为准，`origin` 记住是谁定的）
* `data/library/.text/…` —— **派生物**（抽出来的正文与质量判定，删掉整个目录只会白跑一次抽取）
* 源目录 —— **只读**。这个模块从不往资料根里写一个字节。

1. **正文可用性要"跨篇多点采样"判，不能只看开头**。这是我自己的错，写在这里免得重犯：
   第一版拿**前 4000 字**算字母占比，结果 14 份说明书被判成"抽不出字" ——
   它们的开头是**封面加目录**（满是点线、页码、版本戳），字母占比自然低，而正文是好的。
   改成整篇分五段取中位数之后：**54 / 55 份都可读（中位 54~78%）**。
2. **真的抽不出字的只有 1 份**（`Trefethen-Bau.pdf`），病根在文件本身"没有字形到 Unicode 的映射"：
   `pdffonts` 显示 460 个 **Type 3 + Custom 编码 + `uni: no`** 字体，页面是一堆 1×1 的
   stencil 图片，生成器是 `Aladdin Ghostscript 6.01` —— 典型的**扫描书再转 PDF**。
   基于文字层的工具都抽不出东西，**只能走 OCR**（渲染成位图再识别）；环境里没有
   tesseract / ocrmypdf，所以如实标注"抽不出可用文字"，也不拿它的乱码去推断（那会毁掉引用键）。
3. **判定粒度是"要用的那一行"，不是整篇**：FlashAttention 那篇的符号占比约 48%，
   但**标题行与作者行是干净的** —— 拿整篇印象去否掉局部，会把作者与年份一起丢掉。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import attachments
from .notes import parse_query, search_row

#: 元数据文件的扩展名（`data/library/{citekey}.yaml`）。
METADATA_SUFFIX = ".yaml"

#: 派生目录：抽出来的正文 + 质量判定。**可整体删除**（下次用会自己重抽）。
TEXT_DIR = ".text"

#: 扫描时跳过的目录：版本库、缓存、我们的派生目录。实测资料根里有 `cuda/__pycache__`。
SKIP_DIRS = frozenset({".git", ".svn", "__pycache__", ".obsidian", ".trash", ".text", "node_modules"})

#: 不给当条目看的文件（中间产物）。
SKIP_SUFFIXES = frozenset({".pyc", ".pyo", ".class", ".o", ".so", ".dll", ".dylib", ".lock", ".tmp", ".swp"})

#: 主文件类型 → 条目类别。其余一律 `other`（不丢，只是没话好说）。
KIND_BY_SUFFIX = {
    ".pdf": "paper",
    ".md": "note",
    ".markdown": "note",
    ".txt": "note",
    ".html": "webpage",
    ".htm": "webpage",
    ".py": "code",
    ".c": "code",
    ".h": "code",
    ".cpp": "code",
    ".cc": "code",
    ".hpp": "code",
    ".rs": "code",
    ".go": "code",
    ".js": "code",
    ".ts": "code",
    ".sh": "code",
    ".v": "code",
    ".sv": "code",
    ".ipynb": "code",
    ".tex": "paper",
    ".rst": "note",
}

#: 能从正文里读出附属资源引用的类型（附件归拢只对这几种做）。
ASSET_AWARE = frozenset({".md", ".markdown", ".html", ".htm", ".tex", ".rst"})

#: 整篇正文的可用性分档（字母/汉字占比）。实测：好的 74~82%，坏的 20~22%，
#: 中间那档（30~55%）常是"数字与符号很多的技术文档"，仍可能用 —— 所以它**不是**判死线，
#: 真正的把关在 `_looks_like_prose`（逐行）。
TEXT_OK_RATIO = 0.55
TEXT_POOR_RATIO = 0.30

#: 一份正文最多留多少字（PDF 里几百万字的规范，检索也不需要全留）。
TEXT_LIMIT = 400_000

#: 从正文里读附属资源引用的容量（只看头部，够大多数文档）。
ASSET_SCAN_LIMIT = 200_000

_IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".avif"})

_MONTHS = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)

#: 封面印章 / 版权页 / 版本戳这类噪声行。实测踩过：CXL 那份 PDF 的第一"标题行"
#: 就是 `Evaluation Copy`，照它起键会得到 `evaluationcopy2026`。
_BOILERPLATE = re.compile(
    r"evaluation copy|all rights reserved|copyright|confidential|do not (distribute|copy)|"
    r"^draft\b|^preliminary\b|^version\b|^rev(ision)?\b|^page \d|^\d+$|"
    r"^(" + "|".join(_MONTHS) + r")\b|"
    r"^\w+ \d{1,2}, \d{4}$",
    re.IGNORECASE,
)

_STOPWORDS = frozenset(
    {"the", "and", "for", "with", "from", "into", "using", "based", "guide", "manual", "book", "notes", "paper"}
)

_ARXIV = re.compile(r"^(?P<id>\d{4}\.\d{4,5})(?:v(?P<ver>\d+))?$")
_ARXIV_ANY = re.compile(r"\b(\d{4}\.\d{4,5})(v\d+)?\b")
_YEAR = re.compile(r"(?<!\d)(19[5-9]\d|20[0-4]\d)(?!\d)")
_REPORT_ID = re.compile(r"^(?:[A-Z]{2,6}[-_]?\d{2,4}[-_.]\d{2,6}|EECS-\d{4}-\d+)$", re.IGNORECASE)


class LibraryError(Exception):
    """资料库层面的错误（交给路由转成 400）。"""


@dataclass
class Item:
    """一个资料条目：一个主文件（可能还带它引用的附属资源）。"""

    path: Path
    root: Path
    rel: str
    size: int
    mtime: float

    @property
    def suffix(self) -> str:
        return self.path.suffix.lower()

    @property
    def kind(self) -> str:
        return KIND_BY_SUFFIX.get(self.suffix, "other")

    @property
    def stem(self) -> str:
        return self.path.stem


@dataclass
class Entry:
    """条目 + 它的元数据（元数据可能还没落盘，那时是推断结果）。"""

    item: Item
    meta: dict[str, Any] = field(default_factory=dict)
    assets: list[Path] = field(default_factory=list)
    text_state: dict[str, Any] = field(default_factory=dict)

    @property
    def citekey(self) -> str:
        return str(self.meta.get("citekey") or "")

    def brief(self) -> dict[str, Any]:
        """给列表页的一行。不带正文。"""
        return {
            "citekey": self.citekey,
            "title": str(self.meta.get("title") or self.item.stem),
            "authors": [str(name) for name in (self.meta.get("authors") or [])],
            "year": self.meta.get("year") or 0,
            "kind": str(self.meta.get("kind") or self.item.kind),
            "topics": [str(tag) for tag in (self.meta.get("topics") or [])],
            "origin": str(self.meta.get("origin") or "inferred"),
            "path": str(self.item.path),
            "rel": self.item.rel,
            "bytes": self.item.size,
            "mtime": self.item.mtime,
            "assets": len(self.assets),
            "text": {
                "state": str(self.text_state.get("state") or "none"),
                "chars": int(self.text_state.get("chars") or 0),
                "ratio": float(self.text_state.get("ratio") or 0.0),
            },
            "arxiv": str(self.meta.get("arxiv") or ""),
        }


# ------------------------------------------------------------------ 扫描


def scan(root: Path) -> list[Item]:
    """扫一个根下的所有条目（主文件）。跳过隐藏/缓存目录与中间产物。"""
    root = Path(root)
    if not root.is_dir():
        return []
    out: list[Item] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS or part.startswith(".") for part in path.relative_to(root).parts[:-1]):
            continue
        if path.name.startswith(".") or path.suffix.lower() in SKIP_SUFFIXES:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        out.append(
            Item(path=path, root=root, rel=path.relative_to(root).as_posix(), size=stat.st_size, mtime=stat.st_mtime)
        )
    return out


def assets_of(item: Item) -> list[Path]:
    """主文件里引用到的附属资源（图片等）。

    只对**能读懂引用的文本类**主文件做（md / html / tex / rst）——
    PDF 里的图片嵌在文件内部，不存在"外部引用"这回事。
    """
    if item.suffix not in ASSET_AWARE:
        return []
    try:
        raw = item.path.read_bytes()[:ASSET_SCAN_LIMIT]
    except OSError:
        return []
    text = raw.decode("utf-8", errors="replace")
    found: list[Path] = []
    seen: set[str] = set()
    patterns = (
        r"!\[[^\]]*\]\(([^)\s]+)",                       # markdown 图片
        r"<img[^>]+src=[\"']([^\"']+)",                   # html
        r"\\includegraphics(?:\[[^\]]*\])?\{([^}]+)\}",   # latex
    )
    for pattern in patterns:
        for hit in re.findall(pattern, text):
            target = hit.split("#")[0].split("?")[0].strip()
            if not target or target.startswith(("http://", "https://", "data:")):
                continue
            path = (item.path.parent / target).resolve()
            if path.is_file() and path.suffix.lower() in _IMAGE_EXTS and str(path) not in seen:
                seen.add(str(path))
                found.append(path)
    return found


# ------------------------------------------------------------------ 逐行判定


def _looks_like_prose(line: str) -> bool:
    """这一行像不像"人写的标题/作者"。

    **逐行**判定而不是看整篇：实测同一份 PDF 里，标题行干净、正文行满是符号占位；
    拿整篇的字母占比去否掉它，会把作者与年份一起丢了（FlashAttention 那篇整篇 48% 就是这样）。
    """
    text = (line or "").strip()
    if not (8 <= len(text) <= 200):
        return False
    if _BOILERPLATE.search(text):
        return False
    letters = sum(1 for char in text if char.isalpha() or "\u4e00" <= char <= "\u9fff" or char in " ,.-'&")
    return letters / max(1, len(text)) >= 0.78


def _looks_like_authors(line: str) -> bool:
    """这一行像不像"作者署名"。

    三条硬约束，都是踩出来的：

    * **必须有逗号** —— 作者是一串人名，几位之间用逗号分隔；
    * **不许有冒号** —— 论文标题常写成 `FlashAttention: Fast and … with IO-Awareness`，
      只看"有 and 有逗号"会把**标题**当成作者（实测就是这样把标题与作者写反的）；
    * **至少要两个像人名的片段** —— 版权页的 `COMPUTE EXPRESS LINK CONSORTIUM, INC.`
      只有一个片段、而且全大写，不是作者。

    宁可返回 False（作者留空、等模型或人补）也不要写一个错的作者进元数据。
    """
    text = (line or "").strip()
    if not _looks_like_prose(text) or len(text) > 200:
        return False
    low = text.lower()
    if any(month in low for month in _MONTHS) or ":" in text:
        return False
    if text == text.upper():                       # 全大写 = 机构名/页眉，不是署名
        return False
    if re.search(r"^(department|university|institute|school|college|abstract|keywords)\b", low):
        return False
    if "," not in text:
        return False
    pieces = [piece.strip().strip("†‡*") for piece in re.split(r",| and ", text)]
    names = [piece for piece in pieces if _looks_like_a_person(piece)]
    return len(names) >= 2


#: 机构后缀：`… Consortium, Inc.` 这种前导声明里全是这些词，不该当人名。
_ORG_WORDS = frozenset({"inc", "ltd", "corp", "corporation", "consortium", "university", "institute", "llc", "gmbh"})


def _looks_like_a_person(piece: str) -> bool:
    """一个片段像不像**人名**。

    两条约束覆盖了实测里的两类误判，都是从真数据里抠出来的：

    * **不许含数字** —— IEEE 那份商标页的 `New York, NY 10016-` 会被当成人名；
    * **不许超过 3 个词、不许是机构词** —— CXL 那份的版权声明
      `Compute Express Link Consortium, Inc.` 有 4 个词且带 `Consortium`。
    """
    text = (piece or "").strip().strip("†‡*")
    if not (2 < len(text) < 60) or not text[:1].isupper():
        return False
    if re.search(r"\d", text):
        return False
    words = text.split()
    if not (1 <= len(words) <= 3):
        return False
    return not any(word.strip(".,").lower() in _ORG_WORDS for word in words)


# ------------------------------------------------------------------ 元数据推断


def _pretty(stem: str) -> str:
    """文件名 → 能读的标题：去掉版本尾巴、把连字符与下划线换成空格。"""
    text = re.sub(r"[_-]+", " ", stem)
    text = re.sub(r"\s+v\d+$", "", text)
    return re.sub(r"\s+", " ", text).strip()


def infer(item: Item, *, head_text: str = "") -> dict[str, Any]:
    """推断元数据。**不猜的就不写** —— 宁可字段空着让界面标"待补"，也不要编一个错的。

    顺序：文件名里的确定性标识（arXiv 号 / 年份 / 报告号）→ PDF 首页正文 → 目录名当主题。

    调用方只在该文件正文抽取可用时才传 `head_text`（见 `head_text_for`）。
    """
    meta: dict[str, Any] = {"kind": item.kind, "origin": "inferred"}
    title = ""
    authors: list[str] = []

    arxiv = _ARXIV.match(item.stem) or _ARXIV_ANY.search(item.stem)
    if arxiv:
        meta["arxiv"] = arxiv.group(1)
        # arXiv 号的 `2205` 就是 2022 年 5 月 —— 比在正文里猜年份可靠得多
        meta["year"] = 2000 + int(arxiv.group(1)[:2])
        meta["kind"] = "paper"
    else:
        year = _YEAR.search(item.stem)
        if year:
            meta["year"] = int(year.group(1))
        if _REPORT_ID.match(item.stem):
            meta["kind"] = "report"

    if head_text:
        lines = [line.strip() for line in head_text.splitlines() if line.strip()]

        # 作者行先找：它顺带标出了"标题在上面"的边界
        author_at = -1
        for index, line in enumerate(lines[:22]):
            if _looks_like_authors(line):
                author_at = index
                authors = [
                    name.strip().strip("†‡*0123456789 ")
                    for name in re.split(r",| and ", line)
                    if 2 < len(name.strip()) < 60
                ][:8]
                break

        # 标题 = 作者行**上面那一段连续的散文行**，join 起来。
        # 实测踩过：论文标题常跨两三行（FlashAttention 那篇抽出来是
        # "FlashAttention: Fast and Memory-Efficient Exact Attention" + "with IO-Awareness"），
        # 只取一行会得到 "with IO-Awareness" 这种半截标题，引用键也跟着变成 `dao2022ioawareness`。
        if author_at > 0:
            pieces: list[str] = []
            index = author_at - 1
            while index >= 0 and len(pieces) < 4 and _looks_like_prose(lines[index]):
                if lines[index].startswith(("http", "arXiv:", "doi:")):
                    break
                pieces.insert(0, re.sub(r"\s+", " ", lines[index]))
                index -= 1
            title = " ".join(pieces)[:180]
        if not title:
            for line in lines[:14]:
                if line.startswith(("http", "arXiv:", "doi:")):
                    continue
                if _looks_like_prose(line) and not _looks_like_authors(line):
                    title = re.sub(r"\s+", " ", line)[:180]
                    break

    if title:
        meta["title"] = title
    elif meta.get("arxiv"):
        meta["title"] = "arXiv " + str(meta["arxiv"])       # 比顶着一串数字好看
    else:
        meta["title"] = _pretty(item.stem)
    if authors:
        meta["authors"] = authors
    topics = [part for part in item.rel.split("/")[:-1] if part and not part.startswith(".")]
    if topics:
        meta["topics"] = topics[:4]
    return meta


def _slug_words(text: str, *, words: int = 2, limit: int = 20) -> str:
    """从一串文字里取几个 **ASCII 词**拼成短标识。

    只认 ASCII 词是刻意的：引用键会进 URL、进 YAML、被别的笔记引用，
    混进汉字（`linux命令行与shell`）在别处就是麻烦。一个 ASCII 词都没有
    （纯中文书名）时退到哈希兜底 —— 那种本来就该由人来起个键。
    """
    every = [word.lower() for word in re.findall(r"[A-Za-z]{2,}", text)]
    picked = [word for word in every if word not in _STOPWORDS][:words]
    if not picked:
        # 整个名字都是停用词（实测 `C manual.pdf`）—— 那也比退成哈希强：
        # `cmanual` 一眼认得出是哪一份，`doc-5fcb5d89` 认不出。
        picked = every[:words]
    return "".join(picked)[:limit]


def citekey_for(meta: dict[str, Any], *, fallback: str) -> str:
    """稳定引用键。

    优先序（**照真实语料定的**，每条都有实测依据）：

    1. **有真作者** → `首作者 + 年份 + 标题里的短词`（计划的形状）：实测 `2205.14135v2.pdf`
       抽出来是 `dao2022flashattention` —— 作者 Dao、2022，一眼认得出是 FlashAttention。
    2. **arXiv 号** → `arxiv2205.14135`：语料里一大半是 arXiv 论文，比 `doc-9f3ab2c1`
       好记也好认（这是对计划的一处**刻意偏离**：计划只写了"取不到就 doc- 加哈希"）。
    3. **文件名骨架** → `cxlspecificationrev2026` / `trefethenbau` / `ieee13642005`：
       文件名是这批语料里最稳的信息源。踩过才这么定：CXL 那份 PDF 的"标题行"
       抽出来是封面印章 `Evaluation Copy`，照它起键会得到 `evaluationcopy2026`。
    4. 都不行 → `doc-` 加 8 位哈希（计划的兜底）。

    **键一旦冻结就不要改**：它会被别的笔记引用（`referencing_notes`），改名等于断引用。
    所以流程是「先抽正文、再冻结键」；事后有了更准的信息，由人决定要不要重建，不偷偷改。
    """
    year = str(meta.get("year") or "")
    names = meta.get("authors") or []
    if names:
        author = _slug_words(str(names[0]).split()[-1], words=1, limit=14)
        if author:
            short = _slug_words(str(meta.get("title") or ""), words=3, limit=20)
            return (author + year + short)[:48]
    if meta.get("arxiv"):
        return "arxiv" + str(meta["arxiv"]).replace(".", "").strip()
    base = _slug_words(Path(str(fallback)).stem, words=3, limit=20)
    if base:
        return (base + (year if year and year not in base else ""))[:48]
    return "doc-" + hashlib.sha1(fallback.encode("utf-8")).hexdigest()[:8]


# ------------------------------------------------------------------ 元数据落盘


def _safe_key(citekey: str) -> str:
    key = re.sub(r"[^A-Za-z0-9_.-]+", "-", citekey).strip("-.")
    if not key:
        raise LibraryError("引用键不能是空的")
    return key[:80]


def _meta_path(meta_dir: Path, citekey: str) -> Path:
    return Path(meta_dir) / f"{_safe_key(citekey)}{METADATA_SUFFIX}"


def load_all_metadata(meta_dir: Path) -> dict[str, dict[str, Any]]:
    """把 `data/library/*.yaml` 全读进来（引用键 → 元数据）。文件坏了就跳过，不炸。"""
    out: dict[str, dict[str, Any]] = {}
    folder = Path(meta_dir)
    if not folder.is_dir():
        return out
    for path in sorted(folder.glob("*" + METADATA_SUFFIX)):
        try:
            data = read_metadata(path)
        except (OSError, ValueError):
            continue
        key = str(data.get("citekey") or path.stem)
        data["citekey"] = key
        out[key] = data
    return out


def read_metadata(path: Path) -> dict[str, Any]:
    """读一份元数据。

    自己按 `key: value` 读，不引 YAML 库 —— 这份文件是**我们自己写的**，
    形状就那么几种（标量、方括号列表、花括号映射）；引一个依赖来读自己写的东西，
    换来的是版本兼容问题，收益是零。
    """
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    out: dict[str, Any] = {}
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        head, sep, tail = line.partition(":")
        if not sep:
            continue
        key, value = head.strip(), tail.strip()
        if not key:
            continue
        if value.startswith("[") and value.endswith("]"):
            out[key] = [item.strip().strip("'\"") for item in value[1:-1].split(",") if item.strip()]
        elif value.startswith("{") and value.endswith("}"):
            out[key] = {
                k.strip(): v.strip().strip("'\"")
                for k, _, v in (piece.partition(":") for piece in value[1:-1].split(","))
                if k.strip()
            }
        elif value.isdigit():
            out[key] = int(value)
        elif value:
            out[key] = value.strip("'\"")
    return out


def metadata_index(meta_dir: Path) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """元数据的两个入口：`(按引用键, 按源文件绝对路径)`。

    **为什么必须按路径也建一份**：引用键是**冻结**的，它可能跟"现在临时猜出来的键"不一样 ——
    实测就是这样：`2205.14135v2.pdf` 先猜到 `arxiv220514135`，抽完正文冻结成
    `dao2022flashattention`。列表时若只按键查，这份元数据**永远查不到**，
    表现是"索引跑完了，界面上标题与作者还是空的"。按路径查没有这个问题。
    """
    by_key = load_all_metadata(meta_dir)
    by_source: dict[str, dict[str, Any]] = {}
    for meta in by_key.values():
        source = str(meta.get("source") or "").strip()
        if source:
            by_source[source] = meta
    return by_key, by_source


def save_metadata(meta_dir: Path, meta: dict[str, Any], *, origin: str = "") -> dict[str, Any]:
    """写一份元数据。`origin` 记的是**这一版是谁定的**（推断 / 模型 / 手工）。"""
    citekey = str(meta.get("citekey") or "").strip()
    if not citekey:
        raise LibraryError("元数据里得有 citekey")
    safe = _safe_key(citekey)
    out = dict(meta)
    out["citekey"] = safe
    if origin:
        out["origin"] = origin
    folder = Path(meta_dir)
    folder.mkdir(parents=True, exist_ok=True)
    path = _meta_path(folder, safe)
    order = ("citekey", "title", "authors", "year", "kind", "topics", "arxiv", "source", "origin", "files")
    lines: list[str] = []
    for key in order:
        if key in out:
            lines.extend(_yaml_lines(key, out[key]))
    for key in sorted(out):
        if key not in order:
            lines.extend(_yaml_lines(key, out[key]))
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(path)                       # 原子替换：写到一半断电不会留下半份元数据
    return out


def _yaml_lines(key: str, value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [f"{key}: [{', '.join(_yaml_scalar(item) for item in value)}]"]
    if isinstance(value, dict):
        return [f"{key}: {{{', '.join(f'{k}: {_yaml_scalar(v)}' for k, v in value.items())}}}"]
    return [f"{key}: {_yaml_scalar(value)}"]


def _yaml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if text == "" or re.search(r"[:#\[\]{},]|^\s|\s$", text):
        return "'" + text.replace("'", "''") + "'"
    return text


# ------------------------------------------------------------------ 正文缓存


def _text_paths(text_dir: Path, citekey: str) -> tuple[Path, Path]:
    safe = _safe_key(citekey)
    return Path(text_dir) / f"{safe}.txt", Path(text_dir) / f"{safe}.meta.json"


def text_state(text_dir: Path, citekey: str) -> dict[str, Any]:
    """读**已缓存**的抽取状态（不触发抽取）。"""
    _, sidecar = _text_paths(text_dir, citekey)
    if not sidecar.is_file():
        return {"state": "none", "chars": 0, "ratio": 0.0, "mtime": 0.0}
    try:
        return json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"state": "none", "chars": 0, "ratio": 0.0, "mtime": 0.0}


def text_of(text_dir: Path, citekey: str, *, limit: int = TEXT_LIMIT) -> str:
    path, _ = _text_paths(text_dir, citekey)
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


#: 判正文可用性时采几段。五段够稳，也不快不慢（每段只看 4000 字）。
JUDGE_WINDOWS = 5


#: 判正文可用性时采几段。五段够稳，也不快不慢（每段只看 4000 字）。
JUDGE_WINDOWS = 5

#: "真实词"的门槛（每千字）：ASCII 词（≥3 字母）或汉字词（≥2 字）。
#: 这两条阈值是**量出来的**，不是拍的 —— 详见 `judge_text`。
WORD_OK = 20
WORD_POOR = 8

#: 怪符号占比的上限。真乱码满屏 `!$#%&*`，好文本（哪怕满是表格）几乎为零。
ODD_LIMIT = 0.10

#: 什么算"怪符号"：不在"字母 / 数字 / 汉字 / 空白 / 常规标点"里的字符。
_ODD = re.compile(
    r"[^A-Za-z0-9\u4e00-\u9fff \n\t\r.,;:!?()\[\]{}\"'\-–—/\\%°+*=<>@|~^$`…、。，；：（）「」【】·]"
)
_WORDS = re.compile(r"[A-Za-z]{3,}|[\u4e00-\u9fff]{2,}")


def judge_text(text: str) -> dict[str, Any]:
    """整篇正文的可用性分档。

    两条规矩都是**先量真数据再定**的，写在下面免得下次又想当然：

    1. **不能只看开头**：说明书开头是封面加目录（满是点线、页码），
       只看头 4000 字会把 14 份好文件判死。所以**整篇分五段取中位**。
    2. **不能只看"字母占比"**：表格与图表密集的技术文档（CXL 规范、STM32 参考手册、
       IEEE 标准）字母占比天然只有 10~30%，但它们是**好文本**。
       真正的差别在另外两处（实测对照）：

           指标              7 份好文本        1 份真乱码
           每千字真实词数     13 ~ 46          0
           怪符号占比        0.0% ~ 1.0%      29.3%

       所以判定用「真实词密度 + 怪符号占比」，字母占比只当参考值一并带出。

    `state` 是给人看与给检索排序用的；真正的把关仍是逐行的 `_looks_like_prose`。
    """
    body = text or ""
    if not body.strip():
        return {"state": "none", "chars": len(body), "ratio": 0.0, "words": 0.0, "odd": 0.0, "windows": 0}
    step = max(1, len(body) // JUDGE_WINDOWS)
    letters: list[float] = []
    words: list[float] = []
    odd: list[float] = []
    for index in range(JUDGE_WINDOWS):
        chunk = body[index * step : index * step + 4000]
        if not chunk.strip():
            continue
        letters.append(sum(1 for char in chunk if char.isalpha() or "\u4e00" <= char <= "\u9fff") / len(chunk))
        words.append(len(_WORDS.findall(chunk)) / len(chunk) * 1000)
        odd.append(len(_ODD.findall(chunk)) / len(chunk))
    if not letters:
        return {"state": "none", "chars": len(body), "ratio": 0.0, "words": 0.0, "odd": 0.0, "windows": 0}

    def median(values: list[float]) -> float:
        ordered = sorted(values)
        return ordered[len(ordered) // 2]

    ratio, word_rate, odd_rate = median(letters), median(words), median(odd)
    if odd_rate >= ODD_LIMIT:
        state = "garbled"                      # 怪符号成堆：抽出来的不是字
    elif word_rate >= WORD_OK:
        state = "ok"
    elif word_rate >= WORD_POOR:
        state = "poor"                         # 词少但符号正常：多半是表格密集，能用
    else:
        state = "garbled"                      # 既没词、符号也不像话
    return {
        "state": state,
        "chars": len(body),
        "ratio": round(ratio, 4),
        "headRatio": round(sorted(letters)[0], 4),
        "words": round(word_rate, 1),
        "odd": round(odd_rate, 4),
        "windows": len(letters),
    }


def head_text_for(item: Item, text_dir: Path, citekey: str, *, chars: int = 4000) -> str:
    """给推断用的"首页正文"：**只有可用才给**，乱码一律当空。

    规矩 2（见模块开头）：宁可没有作者/年份（引用键退到文件名），
    也不要拿 `"!$#%` 去推断出一个错的标题与一个毁掉的引用键。

    `poor` 也要给：数学重的论文（实测 FlashAttention 48%，满页符号）整篇占比低，
    但标题与作者那两行是干净的 —— 逐行把关交给 `_looks_like_prose`。
    """
    state = text_state(text_dir, citekey)
    if state.get("state") not in ("ok", "poor"):
        return ""
    return text_of(text_dir, citekey, limit=chars)


def extract_text(item: Item, text_dir: Path, citekey: str, *, force: bool = False) -> dict[str, Any]:
    """抽正文并缓存。**按修改时间增量**：没变过就不重抽（PDF 抽取是最重的 IO）。"""
    path, sidecar = _text_paths(text_dir, citekey)
    state = text_state(text_dir, citekey)
    if not force and path.is_file() and abs(float(state.get("mtime") or 0) - item.mtime) < 1:
        return state
    kind = "pdf" if item.suffix == ".pdf" else ("text" if item.suffix in KIND_BY_SUFFIX else "other")
    raw = attachments.extract(item.path, kind)[:TEXT_LIMIT]
    judged = judge_text(raw)
    judged.update({"mtime": item.mtime, "at": datetime.now().isoformat(timespec="seconds")})
    Path(text_dir).mkdir(parents=True, exist_ok=True)
    path.write_text(raw, encoding="utf-8")
    sidecar.write_text(json.dumps(judged, ensure_ascii=False), encoding="utf-8")
    return judged


# ------------------------------------------------------------------ 组装


def entry_for(item: Item, meta_dir: Path, text_dir: Path, meta: dict[str, Any] | None = None) -> Entry:
    """把一个主文件组装成条目。

    元数据缺失时用**推断结果，但不落盘** —— 落盘是"冻结引用键"这个动作，
    要显式索引或人点头才做（否则每次打开列表都会生成一堆没人确认过的元数据文件）。
    """
    merged = dict(meta or {})
    if not merged:
        guess_key = citekey_for(infer(item), fallback=item.rel)
        head = head_text_for(item, text_dir, guess_key) if item.suffix == ".pdf" else ""
        merged = infer(item, head_text=head)
    merged.setdefault("citekey", citekey_for(merged, fallback=item.rel))
    merged.setdefault("source", str(item.path))
    return Entry(
        item=item,
        meta=merged,
        assets=assets_of(item),
        text_state=text_state(text_dir, str(merged["citekey"])),
    )


def entries(roots: list[Path], meta_dir: Path, text_dir: Path) -> list[Entry]:
    """所有根下的所有条目。引用键冲突时加后缀（`-2`），不让两个条目共用一个键。"""
    by_key, by_source = metadata_index(meta_dir)
    taken: set[str] = set()
    out: list[Entry] = []
    for root in roots:
        for item in scan(Path(root)):
            # 先按**路径**认（键是冻结的，跟临时猜的未必一样）；认不到再按键认一次。
            meta = by_source.get(str(item.path)) or by_key.get(citekey_for(infer(item), fallback=item.rel))
            entry = entry_for(item, meta_dir, text_dir, meta=meta)
            key = entry.meta["citekey"]
            if key in taken:
                base, series = key, 2
                while f"{base}-{series}" in taken:
                    series += 1
                key = f"{base}-{series}"
                entry.meta["citekey"] = key
            taken.add(key)
            out.append(entry)
    return out


# ------------------------------------------------------------------ 检索


def search(roots: list[Path], meta_dir: Path, text_dir: Path, query: str, *, limit: int = 50) -> list[dict[str, Any]]:
    """检索条目。**与笔记同一套表达式**（`#标签` / `title:` / `-词` / `AND` `OR` / `orderby`）。

    正文只在**质量过关**时才进检索池：乱码进去只会让人搜到一堆没用的东西。
    正文差的条目仍然能被元数据（标题、作者、主题）命中 —— 不能因为抽不出字就整个淹没它。
    """
    parsed = parse_query(query or "")
    rows: list[tuple[dict[str, Any], Entry]] = []
    for entry in entries(roots, meta_dir, text_dir):
        body = ""
        if entry.text_state.get("state") in ("ok", "poor"):
            body = text_of(text_dir, entry.citekey, limit=200_000)
        meta = entry.meta
        row = search_row(
            entry.item.rel,
            str(meta.get("title") or entry.item.stem),
            body,
            [str(tag) for tag in (meta.get("topics") or [])],
            kind=str(meta.get("kind") or entry.item.kind),
            modified=entry.item.mtime,
            size=entry.item.size,
            links=len(entry.assets),
            authors=" ".join(str(name) for name in (meta.get("authors") or [])),
            citekey=entry.citekey,
            year=int(meta.get("year") or 0),
        )
        if parsed.match(row):
            rows.append((row, entry))
    if parsed.order:
        rows.sort(key=lambda pair: parsed.sort_key(pair[0]), reverse=parsed.desc)
    return [entry.brief() for _, entry in rows[: (parsed.limit or limit)]]


def referencing_notes(needles: list[str], notes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """哪些笔记引用了这条资料。

    判定规则**写得死**、可以核对：笔记正文里出现引用键，或出现主文件名（含不带后缀的名字）。
    不做模糊匹配 —— "像"的匹配会让"谁引用了它"变成一件说不清的事。
    """
    wanted = [needle for needle in needles if needle]
    if not wanted:
        return []
    out: list[dict[str, Any]] = []
    for note in notes:
        body = str(note.get("body") or "")
        if not body:
            continue
        hit = next((needle for needle in wanted if needle in body), "")
        if hit:
            out.append({"path": str(note.get("path") or ""), "title": str(note.get("title") or ""), "hit": hit})
    return out


def bibtex(meta: dict[str, Any]) -> str:
    """导出一条 BibTeX。字段够用就好，不追求全。"""
    key = str(meta.get("citekey") or "item")
    kind = {"paper": "article", "book": "book", "report": "techreport", "manual": "manual"}.get(
        str(meta.get("kind") or ""), "misc"
    )
    fields = [
        ("title", meta.get("title") or ""),
        ("author", " and ".join(str(name) for name in (meta.get("authors") or []))),
        ("year", meta.get("year") or ""),
        ("eprint", meta.get("arxiv") or ""),
        ("archivePrefix", "arXiv" if meta.get("arxiv") else ""),
        ("keywords", ", ".join(str(tag) for tag in (meta.get("topics") or []))),
        ("file", meta.get("source") or ""),
    ]
    body = ",\n".join(f"  {name} = {{{value}}}" for name, value in fields if value)
    return f"@{kind}{{{key},\n{body}\n}}"


__all__ = [
    "ASSET_AWARE",
    "Entry",
    "Item",
    "KIND_BY_SUFFIX",
    "LibraryError",
    "SKIP_DIRS",
    "TEXT_DIR",
    "TEXT_LIMIT",
    "assets_of",
    "bibtex",
    "citekey_for",
    "entries",
    "entry_for",
    "extract_text",
    "head_text_for",
    "infer",
    "judge_text",
    "load_all_metadata",
    "metadata_index",
    "read_metadata",
    "referencing_notes",
    "save_metadata",
    "scan",
    "search",
    "text_of",
    "text_state",
]
