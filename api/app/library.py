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
import shutil
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

#: **附属资源**：图片、字体、音视频。它们不单独成条目 ——
#: 一个条目 = 一个主文件 + 它引用的附属资源（计划里定死的粒度）。
#: 实测踩过：不给这一层过滤，被正文引用的 `figs/fig-1.svg` 一边正确地进了那条目的
#: `assets`，一边又自己成了列表里的一条；真实语料里 424 张 png 足以把列表淹掉。
#: 刻意**不含** `.pdf` / `.zip`：前者本来就是主文件，后者是"一包东西"，得由人来定。
ASSET_SUFFIXES = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico", ".tif", ".tiff", ".psd",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".mp3", ".wav", ".m4a", ".flac", ".ogg", ".mp4", ".mov", ".avi", ".webm", ".mkv",
})

#: 条目类别的**唯一出处**：提示词（`pipeline/prompts/doc_classify.md`）、
#: 模型返回的校验（`pipeline/doc_classify.py`）与界面标签都从它来。
#: 为什么必须只有一处：加了新类别却漏改一处，就会出现"模型选了但被判成非法"这种事，
#: 而且现象很难查（模型没错、校验说错）。
KINDS = (
    "paper",     # 学术论文、预印本
    "manual",    # 手册与指南：编程指南、最佳实践、用户手册
    "spec",      # 规范与标准：IEEE / IEC / 厂商官方规范
    "report",    # 报告与白皮书
    "book",      # 书与教材
    "slides",    # 幻灯片与课件
    "webpage",   # 网页文档
    "blog",      # 博客与随笔
    "code",      # 代码与脚本
    "note",      # 笔记
    "other",     # 以上都不是
)

#: 类别的中文名（界面用）。
KIND_LABEL = {
    "paper": "论文",
    "manual": "手册",
    "spec": "规范",
    "report": "报告",
    "book": "书",
    "slides": "幻灯片",
    "webpage": "网页",
    "blog": "博客",
    "code": "代码",
    "note": "笔记",
    "other": "其他",
}

#: 文件名的类别线索（确定性、零成本）。**实测这个库里最管用的一步**：
#: `cuda-programming-guide.pdf`、`ptx_isa_9.3.pdf`、`IEEE.1364-2005.pdf` 这类名字
#: 已经把类型写在脸上了，先按它判一道，模型只需处理剩下的。
#: 顺序有讲究：越具体的放前面（`whitepaper` 先于 `report`）。
KIND_HINTS: tuple[tuple[str, str], ...] = (
    (r"specification|\bspec\b|standard|\bieee\b|\biec\b|\bisa\b|\bstd\b|指令集|规范|标准", "spec"),
    (r"whitepaper|white[_-]?paper|白皮书", "report"),
    (r"programming[_-]?guide|best[_-]?practices|user[_-]?guide|\bmanual\b|\bguide\b|tutorial|handbook|手册|指南|入门", "manual"),
    # 注意：探测串里的 `-` `_` `.` 已被归一成空格，所以这里写 `eecs\s*\d` 而不是 `eecs-\d`
    (r"\breport\b|eecs\s*\d|报告", "report"),
    (r"slides|lecture|课件|讲义|ppt", "slides"),
    (r"\bbook\b|textbook|教材|专著|教程", "book"),
    (r"\bblog\b|博客", "blog"),
)

#: 主文件类型 → 条目类别（**只按后缀能确定的那些**）。其余一律 `other`。
#:
#: 注意 `.pdf` 落在这里的 `other` 而不是 `paper` —— 这是用户点出来的一处错：
#: 实测这个库里大量是技术文档（芯片手册、指令集规范、编程指南），一律标"论文"是错的。
#: 类型交给"文件名线索 → 模型判 → 人复核"三步，而不是靠后缀赌一个。
KIND_BY_SUFFIX = {
    ".pdf": "other",
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
    #: 这份条目有没有**冻结过的元数据**（`data/library/*.yaml` 里认得到它）。
    #: 索引要不要跳过它，看的是这个，而不是"键在不在已知表里" —— 两者会分家：
    #: 撞键被改名的那一份，键不在表里、但元数据确实冻结过（实测因此反复重抓）。
    frozen: bool = False
    #: **只有正文**的条目：正文在 `.text/` 里，资料库根目录下却没有它对应的文件
    #: （网站抓下来的页、抽出来的书都是这样进来的）。`path` 就指向那份正文。
    #: 见了它别去写元数据文件、也别当成"库里有这个文件"（见 `text_only_entries`）。
    text_only: bool = False

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
            # 给界面看的来源要算**当前管线判过没有**，不能照抄存的 `origin`：
            # 老数据里它写着 `llm`、内容却是规则法留下的（见 `library_meta.META_REV`），
            # 照抄的话界面会说"判过了"，而列表里一眼就能看出没判。
            "origin": _origin_for_display(self.meta),
            "path": str(self.item.path),
            "rel": self.item.rel,
            "bytes": self.item.size,
            "mtime": self.item.mtime,
            "assets": len(self.assets),
            "text": {
                "state": str(self.text_state.get("state") or "none"),
                "chars": int(self.text_state.get("chars") or 0),
                "ratio": float(self.text_state.get("ratio") or 0.0),
                # **"没抓过" 与 "抓过没有正文" 是两件事**（md / py 这类本来就抽不出东西）。
                # 不带这个标记的话，界面会把已经试过的条目一直显示成"未抓"。
                "attempted": bool(self.text_state.get("at")),
                "words": float(self.text_state.get("words") or 0.0),
                "odd": float(self.text_state.get("odd") or 0.0),
            },
            "arxiv": str(self.meta.get("arxiv") or ""),
            # 这条只有正文（网站页 / 抽出来的书）：界面上要说清"它的原文件不在资料库里"，
            # 否则人点开只看到一份 `.text/…md`，会以为是哪里出错了。
            "textOnly": self.text_only,
        }


# ------------------------------------------------------------------ 扫描


def within(root: Path, target: Path) -> Path:
    """把绝对路径钉在根内 —— 这是**用户的真实文件夹**，写操作必须挡住越界。

    与 `notelib.safe_path` 同一个讲究：路径来自 HTTP，`..` 或绝对路径都能构造出
    根外面的位置，一个手滑就动了别处的文件。
    """
    resolved = Path(target).expanduser().resolve()
    base = Path(root).expanduser().resolve()
    if resolved != base and base not in resolved.parents:
        raise LibraryError(f"这个位置不在资料根里：{target}")
    return resolved


def mkdir(root: Path, dir_path: str) -> str:
    """在根下新建一个目录 —— **真的 mkdir**。

    组织树画的就是磁盘上的目录树，所以"新建文件夹"这件事没有中间层：
    建完它就是用户文件夹里真实存在的目录（用别的文件管理器也看得见）。
    """
    base = Path(root).expanduser().resolve()
    target = within(root, Path(dir_path))
    if target.exists():
        raise LibraryError(f"已经有了：{target.name}")
    target.mkdir(parents=True)
    return target.relative_to(base).as_posix()


def move(root: Path, path: str, to_dir: str) -> str:
    """把一个文件（或目录）挪进另一个目录 —— **真的 move**。

    拒绝三件事，都是真会咬人的：挪到根外、把目录挪进它自己里面、目标位置已存在同名。
    同名不覆盖：这一层的文件是用户的，静默覆盖等于替他删东西。
    """
    base = Path(root).expanduser().resolve()
    src = within(root, Path(path))
    if not src.exists():
        raise LibraryError(f"找不到：{path}")
    dst_dir = within(root, Path(to_dir))
    if not dst_dir.is_dir():
        raise LibraryError(f"这儿不是目录：{to_dir}")
    if dst_dir == src.parent:
        return src.relative_to(base).as_posix()
    if src.is_dir() and (dst_dir == src or src in dst_dir.parents):
        raise LibraryError("不能把一个目录挪进它自己里面")
    dst = dst_dir / src.name
    if dst.exists():
        raise LibraryError(f"目标位置已经有同名的：{src.name}")
    shutil.move(str(src), str(dst))
    return dst.relative_to(base).as_posix()


def safe_name(name: str) -> str:
    """把拖进来的文件名弄干净：只取最后一段、不留路径分隔与 .. 。

    名字来自浏览器，不能直接当路径用（`../../x` 这种在本地应用里照样会咬人）。
    """
    base = Path(str(name or "").replace("\\", "/")).name.strip()
    base = base.replace("/", "_").strip()
    if not base or base in (".", ".."):
        raise LibraryError("文件名不合法")
    return base


def unique_in(folder: Path, name: str) -> Path:
    """同名**不覆盖**：`x.pdf` 已有就写 `x 2.pdf`、`x 3.pdf` …

    这是用户的文件。静默覆盖等于替他删东西 —— 与 `move` 那边同一条讲究。
    """
    stem, suffix = Path(name).stem, Path(name).suffix
    target = folder / name
    series = 2
    while target.exists():
        target = folder / f"{stem} {series}{suffix}"
        series += 1
    return target


def walk(root: Path) -> list[Path]:
    """遍历一个根下**值得看的**文件（跳过隐藏/缓存目录与中间产物）。

    `scan` 与 `scan_media` 共用这一个口径。两处各写一遍的话，"条目数 + 资源数"
    就会时不时对不上，而那种对不上很难查。
    """
    base = Path(root)
    if not base.is_dir():
        return []
    out: list[Path] = []
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS or part.startswith(".") for part in path.relative_to(base).parts[:-1]):
            continue
        if path.name.startswith(".") or path.suffix.lower() in SKIP_SUFFIXES:
            continue
        out.append(path)
    return out


def scan(root: Path) -> list[Item]:
    """扫一个根下的所有条目（**主文件**）。

    附属资源（图片/字体/媒体）**不算条目** —— 它们挂在引用它的那条目的 `assets` 上
    （见 `ASSET_SUFFIXES` 那段注释）。想看资源文件自己有多少个，用 `scan_media`。
    """
    base = Path(root)
    out: list[Item] = []
    for path in walk(base):
        if path.suffix.lower() in ASSET_SUFFIXES:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        out.append(
            Item(path=path, root=base, rel=path.relative_to(base).as_posix(), size=stat.st_size, mtime=stat.st_mtime)
        )
    return out


def scan_media(root: Path) -> list[Path]:
    """扫一个根下的**附属资源文件**（图片/字体/媒体）。

    它们不单独成条目，但也不该**悄悄消失** —— 接口用这个数报一句"另有 N 个资源文件"，
    用户才知道它们没被吞掉（未归拢的那些尤其该让人看见）。
    """
    return [path for path in walk(root) if path.suffix.lower() in ASSET_SUFFIXES]


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


#: 目录里的点引线：`. . . . .`（PDF 抽出来常常是"点 + 空格"交替）
_TOC_DOTS = re.compile(r"\.\s*\.\s*\.(\s*\.)?")
#: 目录行开头的编号：`2.2`、`2.2.1`、`1`（后面跟标题）
_TOC_NUMBER = re.compile(r"^\s*\d+(\.\d+)*\s+\S")
#: 目录行的页码尾巴：标题后面一串点再跟一个数字
_TOC_TAIL = re.compile(r"[.\s]{4,}\d{1,4}\s*$")


def _looks_like_toc(line: str) -> bool:
    """这一行是不是**目录里的一行**。

    为什么要单认它：PDF 抽出来的第一页常常是"封面 + 目录"，而目录行既有很多逗号、
    又有大写词，署名那套判据会把它当作者行（实测那份 CUDA 白皮书：标题被写成
    一整片目录、作者被写成 `Assess, Parallelize, Optimize`）。

    三条形状，任一条命中就算：点引线（`. . . .`）、"编号 + 标题"的开头、
    "标题 …… 页码"的尾巴。
    """
    text = (line or "").rstrip()
    if not text:
        return False
    if _TOC_DOTS.search(text):
        return True
    if _TOC_TAIL.search(text):
        return True
    return bool(_TOC_NUMBER.match(text)) and len(text) < 120


#: 一个「人名对」：`Kiyoshi Honda`、`Yoshiaki Fukazawa` 这种「大写词 + 大写词」。
#: 两个词都要求首字母大写、其余小写、长度合理 —— 全大写的机构名（`WASEDA UNIVERSITY`）
#: 与带数字的目录行都不会命中。
_NAME_PAIR = re.compile(r"\b[A-Z][a-z]{1,15}\s+[A-Z][a-z]{1,15}\b")


def _name_pairs(line: str) -> list[str]:
    """这一行里出现的所有人名对（`Kiyoshi Honda` 这种）。"""
    return _NAME_PAIR.findall(line or "")


def _looks_like_name_line(line: str) -> bool:
    """这一行是不是**一串人名**（可能是没有逗号的署名行）。

    为什么单独认：署名行不一定用逗号分隔 —— PDF 抽出来常常是
    `Kiyoshi Honda Hironori Washizaki Yoshiaki Fukazawa`（空格隔开）。
    这种行既过不了"必须有逗号"那条，又会被当成标题的后续行拼进去，
    实测标题于是成了"标题 + 一串人名"。

    判据：连着三个以上人名对、且这一行没有逗号（有逗号的走 `_looks_like_authors`）。
    """
    text = (line or "").strip()
    if not text or "," in text or len(text) > 160:
        return False
    words = text.split()
    if len(words) < 6:
        return False
    # 人名对必须**首尾相接**地铺满整行（`Kiyoshi Honda Hironori Washizaki Yoshiaki Fukazawa`）。
    # 踩过：只看"三个以上人名对"会把 **Title Case 的标题**也认成署名行 ——
    # `An Empirical Study on Predicting Software Development Bugs` 里有
    # `Empirical Study`、`Predicting Software`、`Development Bugs` 三对，
    # 于是标题行成了"署名行"、真正的作者行被跳过，结果作者一个都没留下
    # （被 `test_infer_drops_place_names_from_a_real_signature_block` 抓到）。
    # 标题里的大写词之间总夹着小写虚词（`Study **on** Predicting`），
    # 所以"接不上就停"这一条能把两者分开。
    at = 0
    pairs = 0
    while at + 1 < len(words) and _NAME_PAIR.fullmatch(words[at] + " " + words[at + 1]):
        pairs += 1
        at += 2
    return pairs >= 3 and at >= len(words) - 1


def _looks_like_authors(line: str) -> bool:
    """这一行像不像"作者署名"。

    四条硬约束，都是踩出来的：

    * **必须有逗号** —— 作者是一串人名，几位之间用逗号分隔；
    * **不许有冒号** —— 论文标题常写成 `FlashAttention: Fast and … with IO-Awareness`，
      只看"有 and 有逗号"会把**标题**当成作者（实测就是这样把标题与作者写反的）；
    * **至少要两个像人名的片段** —— 版权页的 `COMPUTE EXPRESS LINK CONSORTIUM, INC.`
      只有一个片段、而且全大写，不是作者；
    * **目录行不算** —— ` 2.2 Assess, Parallelize, Optimize, Deploy . . .` 两个条件都满足，
      但它是目录（见 `_looks_like_toc`）。

    宁可返回 False（作者留空、等模型或人补）也不要写一个错的作者进元数据。
    """
    text = (line or "").strip()
    if not _looks_like_prose(text) or len(text) > 200:
        return False
    if _looks_like_toc(text):
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
_ORG_WORDS = frozenset({
    "inc", "ltd", "corp", "corporation", "consortium", "university", "institute", "llc", "gmbh",
    # 机构名的常见碎块（实测从"署名区块"里被切出来过）
    "department", "faculty", "college", "school", "hospital", "laboratory", "centre", "center",
    "medicine", "technology", "science", "sciences", "engineering", "research", "group",
    # 出版社（实测 `O'Reilly Media`、`Springer` 被当过作者）
    "media", "press", "publishing", "publishers", "books", "editions", "verlag",
    "springer", "wiley", "manning", "packt", "oreilly", "oreillymedia", "apress", "addison",
    # 地址后缀（实测 `Ambassador House` / `Concord Business Park` / `Threapwood Road`）
    "house", "park", "road", "street", "avenue", "drive", "lane", "boulevard", "plaza",
    "suite", "floor", "building", "floor",
})

#: 虚词：`Laws of` 这种"标题碎片"以它结尾，肯定不是人名。
_FUNCTION_WORDS = frozenset({
    "of", "and", "the", "for", "with", "in", "on", "at", "by", "to", "from", "vs", "via",
    "into", "over", "under", "between", "through", "as", "or", "an", "a", "is", "are",
})

#: 中文机构标记：`授权人民邮电出版社出版` 这种片段（中文姓名没有空格，
#: 所以"至少两个词"那条卡不住它，得靠这几个词认出来）。
_CN_ORG = re.compile(r"(出版社|出版|公司|集团|大学|学院|研究所|研究院|图书馆|授权)")

#: 这些词打头的"人名"是说明文字（`The`、`Figure`、`Thereafter`……）。
_NOT_NAMES = frozenset({
    "the", "this", "these", "that", "those", "there", "thereafter", "then", "than",
    "figure", "fig", "table", "section", "chapter", "appendix", "references", "bibliography",
    "abstract", "keywords", "contents", "index", "preface", "overview", "release", "version",
    "draft", "revision", "note", "notes", "since", "when", "where", "while", "page", "vol",
    "no", "et", "al", "see", "also", "however", "thus", "hence", "following", "under",
})

#: 中日韩字：中文姓名没有空格可分（`王小明`），不能拿"至少两个词"去卡它。
_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")


#: 地名与国家名：署名区块里紧跟着 institutions 的那些。
#: 判据是"**这个词都算**"（见 `_looks_like_a_person`）—— 这样 `Ann Smith` 不会被
#: 误杀，而 `Ann Arbor`、`Japan Tokyo`、单独的 `Osaka` 会被挡掉。
_PLACES = frozenset(
    {
        # 国家与地区
        "new", "japan", "china", "usa", "us", "uk", "england", "scotland", "germany", "france",
        "canada", "india", "korea", "taiwan", "singapore", "italy", "spain", "portugal",
        "netherlands", "belgium", "sweden", "norway", "denmark", "finland", "switzerland",
        "austria", "poland", "czech", "russia", "israel", "iran", "turkey", "egypt",
        "brazil", "mexico", "chile", "argentina", "australia", "zealand", "ireland",
        "greece", "hungary", "romania", "vietnam", "thailand", "malaysia", "indonesia",
        # 常见城市（含研究机构密集的那些）
        "tokyo", "osaka", "kyoto", "nagoya", "sendai", "sapporo", "fukuoka",
        "beijing", "shanghai", "shenzhen", "hangzhou", "nanjing", "wuhan", "chengdu",
        "seoul", "daejeon", "taipei", "hsinchu", "hong", "kong", "macau",
        "bangalore", "bengaluru", "mumbai", "delhi", "chennai", "hyderabad", "pune",
        "london", "oxford", "cambridge", "manchester", "edinburgh", "glasgow",
        "boston", "cambridge", "seattle", "austin", "york", "francisco", "angeles",
        "chicago", "pittsburgh", "arbor", "urbana", "champaign", "diego", "atlanta",
        "paris", "berlin", "munich", "zurich", "vienna", "rome", "milan", "turin",
        "amsterdam", "eindhoven", "stockholm", "oslo", "helsinki", "copenhagen",
        "toronto", "montreal", "vancouver", "ottawa", "sydney", "melbourne", "canberra",
        "moscow", "tel", "aviv", "haifa", "istanbul", "cairo", "sao", "paulo",
    }
)


def _looks_like_a_person(piece: str, title: str = "") -> bool:
    """一个片段像不像**人名**。

    四条约束，每一条都对应真实数据里的一次误判：

    * **不许含数字** —— IEEE 那份商标页的 `New York, NY 10016-` 会被当成人名；
    * **不许超过 3 个词、不许是机构词** —— CXL 那份的版权声明
      `Compute Express Link Consortium, Inc.` 有 4 个词且带 `Consortium`；
    * **全都是地名就不是人名** —— 论文那篇的署名区块按逗号切开后是
      `Osaka` / `Japan Tokyo` / `Japan Tokyo`…… 一串地名；
    * **标题里出现过的不是人名** —— 白皮书封面那句
      `…with Enhanced AI Capabilities, Advanced Precisions, High Efficiency`
      被逗号切开后整段当成三个作者。
    """
    text = (piece or "").strip().strip("†‡*")
    if not (2 < len(text) < 60) or not text[:1].isupper():
        return False
    if re.search(r"\d", text):
        return False
    words = text.split()
    if not (1 <= len(words) <= 3):
        return False
    # 括号 / 斜杠 / 分号：那是机构、缩写或"GR/L"这种编号，不是人名
    if re.search(r"[()\[\]/&;]", text):
        return False
    if _CN_ORG.search(text):
        return False
    if words[0].strip(".,").lower() in _NOT_NAMES:
        return False
    if any(word.strip(".,").lower() in _ORG_WORDS for word in words):
        return False
    # 以虚词开头或结尾的（`Laws of`、`of the`）：标题碎片，不是人名
    if words[0].strip(".,").lower() in _FUNCTION_WORDS:
        return False
    if words[-1].strip(".,").lower() in _FUNCTION_WORDS:
        return False
    # **英文姓名至少要两个词**：`Parallelize`、`Optimize`、`Medicine`、`Technology`
    # 这类单个英文词曾被当成作者写进元数据（实测）。中文姓名没有空格可分，另行放过。
    if len(words) < 2 and not _CJK.search(text):
        return False
    # **任一个词**是地名就否掉（不是"全都要是"）：`Ann Arbor` 里只有 `Arbor` 是地名，
    # 而 `Ann` 本身是常见名 —— 只看"全都是"就会把 Ann Arbor 当成人名。
    # 代价是个别"名 + 地名"的组合会误杀（如 `York Smith`），在这个库里可以接受：
    # 宁可作者留空等人来补，也不要写一个错的进去。
    if any(word.strip(".,").lower() in _PLACES for word in words):
        return False
    return not (title and text.lower() in title.lower())


# ------------------------------------------------------------------ 元数据推断


def _kind_hint(stem: str) -> str:
    """文件名能不能看出类别。看不出返回空串（**不要瞎猜一个**）。

    匹配前把 `_` `-` `.` 归一成空格：正则里 `_` 算词字符，`ptx_isa_9.3` 里的 `isa`
    因此匹配不上 `\bisa\b` —— 实测就是这么漏判了一份规范。
    """
    probe = re.sub(r"[_\-.]+", " ", stem)
    for pattern, kind in KIND_HINTS:
        if re.search(pattern, probe, re.IGNORECASE):
            return kind
    return ""


def _pretty(stem: str) -> str:
    """文件名 → 能读的标题：去掉版本尾巴、把连字符与下划线换成空格。"""
    text = re.sub(r"[_-]+", " ", stem)
    text = re.sub(r"\s+v\d+$", "", text)
    return re.sub(r"\s+", " ", text).strip()


def infer(item: Item, *, head_text: str = "") -> dict[str, Any]:
    """**已不在管线里** —— 元数据现在由 `library_meta` 问模型（用户要求"全部 llm"）。

    留着它有两个用处：它记着这批语料的真实形状（那些注释与判据都是被具体文件打脸
    之后补的），以及测试拿它当基准。**新代码不要再调它**：文件千奇百怪，
    模式匹配是在跟无穷多种排版较劲。
    """
    """推断元数据。**不猜的就不写** —— 宁可字段空着让界面标"待补"，也不要编一个错的。

    顺序：文件名里的确定性标识（arXiv 号 / 年份 / 报告号）→ PDF 首页正文 → 目录名当主题。

    调用方只在该文件正文抽取可用时才传 `head_text`（见 `head_text_for`）。
    """
    meta: dict[str, Any] = {"kind": _kind_hint(item.stem) or item.kind, "origin": "inferred"}
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
            # 目录页要整片跳过：它既不是署名，也不该被当成"署名行上面的标题"
            if _looks_like_toc(line):
                continue
            if _looks_like_authors(line):
                author_at = index
                authors = [
                    name.strip().strip("†‡*0123456789 ")
                    for name in re.split(r",| and ", line)
                    if 2 < len(name.strip()) < 60
                ][:8]
                break
            if _looks_like_name_line(line):
                # 没有逗号的署名行：直接取里面的人名对
                author_at = index
                authors = _name_pairs(line)[:8]
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
                if _looks_like_toc(lines[index]) or _looks_like_name_line(lines[index]):
                    break
                pieces.insert(0, re.sub(r"\s+", " ", lines[index]))
                index -= 1
            title = re.sub(r"\s+", " ", " ".join(pieces)).lstrip("#>*-").strip()[:180]
        if not title:
            for line in lines[:14]:
                if line.startswith(("http", "arXiv:", "doi:")):
                    continue
                # 网址（`www.elsevier.com/locate/ipl`）不能当标题 —— 实测取到过
                if "://" in line or re.search(r"\bwww\.|\.[a-z]{2,4}/", line, re.I):
                    continue
                if _looks_like_toc(line):
                    continue
                if _looks_like_prose(line) and not _looks_like_authors(line):
                    # 剥掉行首的 Markdown 记号：实测 md 文件被当成"首页正文"喂进来时，
                    # 标题取成了 `# Layout polynomials`（那个 `#` 不该进标题）
                    title = re.sub(r"\s+", " ", line.lstrip("#>*-").strip())[:180]
                    break

    if title:
        meta["title"] = title
    elif meta.get("arxiv"):
        meta["title"] = "arXiv " + str(meta["arxiv"])       # 比顶着一串数字好看
    else:
        meta["title"] = _pretty(item.stem)
    if authors:
        # **逐个人名再筛一遍**，而且这次带着标题一起筛。
        # 原先只筛了"整行像不像署名"，而取出来时是**整行按逗号切**的 —— 于是署名
        # 区块里的机构行会被切成一串地名混进来。实测两条真实数据就是这么坏的：
        #   论文那篇 → ["Osaka", "Japan Tokyo", "Japan Tokyo", …]（用户："作者怎么会
        #   是 Osaka 和 Tokyo？"）；
        #   白皮书那篇 → ["Capabilities", "Advanced Precisions", "High Efficiency"]，
        #   那是封面副标题被逗号切开（用户："Capabilities, Advanced Precisions"）。
        meta["authors"] = [name for name in authors if _looks_like_a_person(name, title=title)][:8]
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


def _origin_for_display(meta: dict[str, Any]) -> str:
    """界面上的"这条元数据是谁给的"。

    `manual`（人定的）原样返回；其余按**当前管线**判过没有来分：
    判过 = `llm`，没判过 = `pending`（界面显示"待判"）。
    """
    from . import library_meta  # noqa: PLC0415 —— 放在函数里，避免模块级循环导入

    stored = str(meta.get("origin") or "")
    if stored == "manual":
        return "manual"
    return "pending" if library_meta.looks_unjudged(meta) else (stored or "pending")


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


def disambiguate(key: str, rel: str, taken: set[str] | dict[str, Any]) -> str:
    """键撞了怎么办：**用从路径派生的后缀**，不用流水号。

    踩过两次，都写在这里：
    1. 流水号随扫描顺序变 —— 同一个文件这次 `risc-2`、下次 `risc-4`，
       冻结的元数据与正文缓存再也对不上，索引队列排不空；
    2. 这条规则原先只写在列表（`entries`）里，**冻结时没走同一套**，
       于是两个条目冻结成同一个键、后写的把前一份覆盖掉，前者下一轮又变成"没抓过"。

    所以现在只有这一份实现，列表与冻结都走它。
    """
    if key not in taken:
        return key
    base = key
    tag = _slug_words(str(Path(rel).parent), words=2, limit=10)
    if not tag:
        tag = hashlib.sha1(rel.encode("utf-8")).hexdigest()[:4]
    key = f"{base}-{tag}"
    series = 2
    while key in taken:
        key = f"{base}-{tag}-{series}"
        series += 1
    return key


def freeze(meta_dir: Path, text_dir: Path, meta: dict[str, Any], rel: str, *, origin: str = "inferred") -> dict[str, Any]:
    """冻结一份元数据（第一次给它定键）。

    撞键走 `disambiguate`（与列表同一套规则），并把正文缓存跟着改名 ——
    两件事都不做的话，索引队列会反复重抓同一批文件。
    """
    known = load_all_metadata(meta_dir)
    key = str(meta.get("citekey") or "")
    holder = known.get(key)
    # "这把键被别人占着吗"要按**规范标识**比：同一个文件换了种写法（软链）不算别人，
    # 否则会把自己判成撞键、给自己加一个目录后缀（实测那 7 组就是这么来的）。
    if holder is not None and source_id(holder.get("source")) not in ("", source_id(meta.get("source"))):
        fixed = disambiguate(key, rel, known)
        if fixed != key:
            rename_text(text_dir, key, fixed)
            meta = dict(meta)
            meta["citekey"] = fixed
    return save_metadata(meta_dir, meta, origin=origin)


def source_id(path: Any) -> str:
    """源文件的**规范标识**：解析过软链的绝对路径。

    为什么不能直接拿 `source` 字符串比：同一个文件经常有几种写法
    （`reference/` 是仓库外的软链 —— 记的是软链路径还是解析后的真实路径，
    取决于它是怎么被填进来的）。
    实测就这么长出了 7 组"同一文件两把键"：字符串匹配不上 → 条目回落到重新推断 →
    猜出的键撞上已冻结的那把 → 被加上目录后缀（`…-cuda`），于是那份文件有两份元数据、
    两把键，而笔记里到底引用的是哪一把就说不准了。

    解析失败（文件不在、权限）时原样返回 —— 宁可比不上，也不要抛。
    """
    text = str(path or "").strip()
    if not text:
        return ""
    try:
        return str(Path(text).expanduser().resolve())
    except OSError:
        return text


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
        source = source_id(meta.get("source"))
        if source:
            by_source[source] = meta
    return by_key, by_source


#: 标题开头的标记符号：md 的 `#`、项目符号、引用。
#: **刻意不含"数字编号"**（`^\d+[.)]`）—— 试过，标题以数字开头太常见了：
#: 实测把标准号 `1364.1 TM` 吃成了 `1 TM`。宁可不收拾，也别把真东西咬掉。
_TITLE_MARKS = re.compile(r"^[\s>\-*+\u2022]+|^#{1,6}\s*")


def clean_title(text: Any) -> str:
    """把标题收拾干净：去开头的标记符号、压空白、去首尾。

    三条路都会写标题（推断 / 模型归类 / 人手填），这里收一道 ——
    实测看到列表里顶着 `# Layout polynomials`：那是**模型归类**写的，
    推断那条路本来就有 `lstrip("#>*-")`，模型那条没有。
    """
    cleaned = _TITLE_MARKS.sub("", str(text or "").strip())
    return re.sub(r"\s+", " ", cleaned).strip()[:180]


def save_metadata(meta_dir: Path, meta: dict[str, Any], *, origin: str = "") -> dict[str, Any]:
    """写一份元数据。`origin` 记的是**这一版是谁定的**（推断 / 模型 / 手工）。

    标题在这里统一收拾干净（见 `clean_title`）—— 无论这一版是谁写的。
    """
    citekey = str(meta.get("citekey") or "").strip()
    if not citekey:
        raise LibraryError("元数据里得有 citekey")
    safe = _safe_key(citekey)
    out = dict(meta)
    out["citekey"] = safe
    if out.get("title"):
        out["title"] = clean_title(out["title"])
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


def _text_variants(text_dir: Path, citekey: str) -> tuple[list[Path], list[Path]]:
    """这份正文**可能**落在哪几个文件上：`([正文…], [旁注…])`，按可信度排序。

    两条路留下的名字不一样，都得认：

    * 文件那条（PDF / 手册）→ `<键>.txt` + `<键>.meta.json`；
    * 抓网页与抽书那条 → `<键>.md` + `<键>.json`。

    只认前一种的话，后一种材料正文明明在盘上、检索也查得到，却被判成"没抓过"：
    资料库页里搜不到它的正文、列表里也不出现（实测：网站那 29 页与 10 本书）。
    """
    safe = _safe_key(citekey)
    base = f"{Path(text_dir) / safe}"
    return [Path(base + ".txt"), Path(base + ".md")], [Path(base + ".meta.json"), Path(base + ".json")]


def rename_text(text_dir: Path, old: str, new: str) -> bool:
    """把正文缓存从一个引用键挪到另一个。

    **为什么必须有这个动作**：定键是"抽完正文才定"的（作者与年份在正文里），
    所以抽取时用的那个键可能随后就改了（`arxiv220514135` → `dao2022flashattention`）。
    缓存若按旧键留盘，新的键就永远找不到它 —— 表现为条目一直显示"没抓过"、
    "抓正文"队列反复重抓同几份（实测 60 轮 219 份，全库只有 58 条）。
    """
    if not old or not new or old == new:
        return False
    moved = False
    for suffix in (".txt", ".meta.json"):
        source, target = _text_paths(text_dir, old)[0].with_suffix(suffix), _text_paths(text_dir, new)[0].with_suffix(suffix)
        if source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            source.replace(target)
            moved = True
    return moved


def text_state(text_dir: Path, citekey: str) -> dict[str, Any]:
    """读**已缓存**的抽取状态（不触发抽取）。两种命名都认（见 `_text_variants`）。"""
    for sidecar in _text_variants(text_dir, citekey)[1]:
        if not sidecar.is_file():
            continue
        try:
            return json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return {"state": "none", "chars": 0, "ratio": 0.0, "mtime": 0.0}


def text_of(text_dir: Path, citekey: str, *, limit: int = TEXT_LIMIT) -> str:
    """这份条目的正文（两种命名都认，见 `_text_variants`）。"""
    for path in _text_variants(text_dir, citekey)[0]:
        if not path.is_file():
            continue
        try:
            return path.read_text(encoding="utf-8", errors="replace")[:limit]
        except OSError:
            continue
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
    # kind 交给 `attachments.kind_of` 一处决定：pdf / docx / pptx / text / legacy ——
    # 资料这边不再自己映射一遍（自己映射一遍的话，新加的格式会在这里被漏掉：
    # 现象是"能看但搜不到"，比看不了更隐蔽）。
    kind = attachments.kind_of(item.path.name, "")
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

    元数据缺失时**只给机械信息**（文件名派生的标题、扩展名决定的类型），
    标题/作者/年份/主题留给模型（`library_meta`）—— 用户明确要求"不要规则兜底，
    全部 llm"。这里**不联网、也不读正文**：列表是一次点击就要出来的东西，
    塞一次模型调用进去等于让目录页等一个网络往返。

    落盘仍然要显式索引或人点头（否则每次打开列表都会生成一堆没人确认过的元数据文件）。
    """
    merged = dict(meta or {})
    if not merged:
        merged = {
            "title": _pretty(item.stem),
            "kind": item.kind,
            #: `pending` = 还没让模型判过。界面据此显示"待补"与"用模型重判"。
            "origin": "pending",
        }
    merged.setdefault("citekey", citekey_for(merged, fallback=item.rel))
    merged.setdefault("source", str(item.path))
    return Entry(
        item=item,
        meta=merged,
        assets=assets_of(item),
        text_state=text_state(text_dir, str(merged["citekey"])),
    )


def text_only_entries(text_dir: Path, *, skip: set[str]) -> list[Entry]:
    """**只有正文的条目**：正文在 `.text/` 里，资料库根目录下却没有它对应的文件。

    为什么需要它：网站抓下来的页与抽出来的书是**直接进 `.text/`** 的（原文件在
    `/mnt/f/Website`、`/mnt/f/Books` 这种库外目录里），`scan` 扫根目录当然扫不到 ——
    于是出现"正文进了库、检索查得到，唯独资料库页列不出来"（用户："我这边还查不到"）。
    这里把它们补进列表：`path` 指向 `.text/<键>.md`，点开就是正文。

    `skip` 是资料库里的真文件已经占掉的引用键：**撞键的一律不要**。宁可少列一条，
    也不能让清单里两条共用一把键（引用、笔记、题都按这个键走，共用就是错的）。

    `frozen=False`：它不是资料库里的文件，不该在键的抢占里挤掉真条目；
    也因此**不会**被索引流程当成"有主"去重抓。
    """
    out: list[Entry] = []
    base = Path(text_dir)
    for sidecar in sorted(base.glob("*.json")):
        if sidecar.name.endswith(".meta.json"):
            continue
        try:
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(meta, dict):
            continue
        key = str(meta.get("citekey") or sidecar.stem)
        body = base / f"{key}.md"
        if not key or key in skip or not body.is_file():
            continue
        try:
            stat = body.stat()
        except OSError:
            continue
        merged = dict(meta)
        merged.setdefault("citekey", key)
        out.append(
            Entry(
                item=Item(
                    path=body,
                    root=base,
                    rel=str(body.relative_to(base.parent)),
                    size=stat.st_size,
                    mtime=stat.st_mtime,
                ),
                meta=merged,
                assets=[],
                text_state=text_state(base, key),
                text_only=True,
            )
        )
    return out


def entries(roots: list[Path], meta_dir: Path, text_dir: Path) -> list[Entry]:
    """所有根下的所有条目。引用键冲突时加后缀（`-2`），不让两个条目共用一个键。"""
    by_key, by_source = metadata_index(meta_dir)
    out: list[Entry] = []
    for root in roots:
        for item in scan(Path(root)):
            # 先按**路径**认（键是冻结的，跟临时猜的未必一样）；认不到再按键认一次。
            # 路径要过 `source_id`：同一个文件有软链与真实路径两种写法（实测那 7 组
            # "同一文件两把键"就是这么来的），字符串直接比会认不出来。
            meta = by_source.get(source_id(item.path)) or by_key.get(
                citekey_for(infer(item), fallback=item.rel)
            )
            entry = entry_for(item, meta_dir, text_dir, meta=meta)
            entry.frozen = meta is not None
            out.append(entry)

    # 再补上**只有正文的那种**（网站页、抽出来的书）：原文件在库外，`scan` 扫不到它们。
    # 键已被真文件占掉的一律跳过（见 `text_only_entries`）。
    out.extend(text_only_entries(text_dir, skip={entry.citekey for entry in out}))

    # **冻结过的键先占位**：它是"有主"的，不能被后来的条目挤掉。
    # 踩过：一份手册的键先被旁边的中文版占了，于是它每轮都被改名一次 ——
    # 改完的键在已知表里找不到，索引就一遍遍重抓它（"还剩 0"却永远抓不完）。
    taken: dict[str, Entry] = {}
    for entry in out:
        if entry.frozen:
            taken.setdefault(entry.citekey, entry)
    for entry in out:
        if taken.get(entry.citekey) is entry:
            continue
        key = str(entry.meta["citekey"])
        fixed = disambiguate(key, entry.item.rel, taken)
        if fixed != key:
            entry.meta["citekey"] = fixed
        taken.setdefault(fixed, entry)
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
    "KIND_HINTS",
    "KIND_LABEL",
    "KINDS",
    "LibraryError",
    "SKIP_DIRS",
    "TEXT_DIR",
    "TEXT_LIMIT",
    "assets_of",
    "bibtex",
    "citekey_for",
    "disambiguate",
    "entries",
    "entry_for",
    "extract_text",
    "freeze",
    "head_text_for",
    "infer",
    "judge_text",
    "load_all_metadata",
    "metadata_index",
    "read_metadata",
    "rename_text",
    "referencing_notes",
    "save_metadata",
    "scan",
    "search",
    "text_of",
    "text_state",
]
