"""附件的落盘、正文抽取与读取。

三条纪律：

* **先判体积，再写盘**。超限直接拒，不留半个文件在盘上（写完再删的那种做法，
  一旦进程被杀就会留垃圾）。
* **正文与文件分开**：抽出来的 `text` 进库（给模型看），文件本身躺在盘上
  （给人看）。零件里只放元数据 —— 它是每次读会话都要发给前端的东西。
* **路径相对 `api/`**：绝对路径不进库。材料那一课的教训（换台机器路径就不通）。

正文抽取只做两件事：纯文本直接读、PDF 走 `pdftotext`。图片**不抽**
（那要视觉模型，是另一条线）—— 但附件本身照常存下来给人看。
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

from . import toolchain
from .models import Attachment

# 一个附件最大 12MB：够一份 PDF 或一张截图，又不至于让一次上传把服务打住
MAX_BYTES = 12 * 1024 * 1024
# 抽出来的正文最多留这么多字：它会进模型的上下文，不能无界
TEXT_LIMIT = 200_000
# 单次读给模型的正文上限（比 TEXT_LIMIT 更紧：上下文是按预算算的，不是按库容）
CONTEXT_LIMIT = 12_000

PDF_TIMEOUT = 20  # 秒：pdftotext 卡住时别把整个请求拖死

TEXT_SUFFIXES = {
    ".md", ".markdown", ".txt", ".rst", ".csv", ".tsv", ".json", ".yaml", ".yml",
    ".py", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".c", ".h", ".cc", ".cpp", ".hpp",
    ".rs", ".go", ".java", ".sh", ".sql", ".toml", ".ini", ".cfg", ".log", ".html",
    ".css", ".xml", ".tex",
}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}

#: 用 `pandoc` 能读的文档格式（docx 是 zip + XML，pandoc 直接吃）。
DOC_SUFFIXES = {".docx", ".rtf", ".odt"}
#: 演示文稿：**自己解**（pandoc 读不了 pptx，而它其实就是 zip + XML）。
SLIDE_SUFFIXES = {".pptx"}
#: 老式二进制格式：抽不出可用文字，照实说，别假装。
LEGACY_SUFFIXES = {".doc", ".ppt", ".xls"}

MIME_BY_SUFFIX = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain; charset=utf-8",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".doc": "application/msword",
    ".ppt": "application/vnd.ms-powerpoint",
}


def mime_of(name: str) -> str:
    """按后缀给一个 MIME。认不出就当二进制（浏览器会存下来，不会乱猜怎么渲染）。"""
    return MIME_BY_SUFFIX.get(Path(name or "").suffix.lower(), "application/octet-stream")


def view_kind(name: str) -> str:
    """这份文件该怎么看：`pdf`（浏览器自带阅读器）/ `image` / `markdown` /
    `docx`（pandoc 转 HTML）/ `pptx`（自己解成幻灯片）/ `text`（原样给）/ `legacy` / `binary`。

    `html` **按源码看**：本地 HTML 里可能有脚本，直接渲染等于在我们的源里执行它。
    """
    suffix = Path(name or "").suffix.lower()
    if suffix == ".pdf":
        return "pdf"
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix in (".md", ".markdown"):
        return "markdown"
    if suffix in DOC_SUFFIXES:
        return "docx"
    if suffix in SLIDE_SUFFIXES:
        return "pptx"
    if suffix in LEGACY_SUFFIXES:
        return "legacy"
    if suffix in TEXT_SUFFIXES:
        return "text"
    return "binary"


def uploads_dir() -> Path:
    """附件根目录（相对 `api/`）—— 已在 `.gitignore` 里，绝不进版本库。"""
    return Path(__file__).resolve().parent.parent / "var" / "uploads"


def kind_of(name: str, mime: str) -> str:
    suffix = Path(name or "").suffix.lower()
    if suffix == ".pdf" or "pdf" in (mime or ""):
        return "pdf"
    if suffix in DOC_SUFFIXES:
        return "docx"
    if suffix in SLIDE_SUFFIXES:
        return "pptx"
    if suffix in LEGACY_SUFFIXES:
        return "legacy"
    if suffix in IMAGE_SUFFIXES or (mime or "").startswith("image/"):
        return "image"
    if suffix in TEXT_SUFFIXES or (mime or "").startswith("text/"):
        return "text"
    # 认不出后缀但像纯文本的（json/csv 之类常被浏览器报成别的）也当文本试一把
    return "other"


def _extract_pdf(path: Path) -> str:
    """`pdftotext` 抽 PDF 正文。

    命令从 `toolchain` 拿（系统 → 仓库自带 → 本机缓存 → 首启下载），
    **不是**直接写个名字让 PATH 去猜 —— 猜不到的后果是"文档能打开但一个字都没有"。
    """
    found, _why = toolchain.ensure("pdftotext")
    if found is None:
        return ""
    try:
        done = subprocess.run(
            [str(found), "-layout", "-q", str(path), "-"],
            capture_output=True,
            timeout=PDF_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if done.returncode != 0:
        return ""
    return done.stdout.decode("utf-8", errors="replace")[:TEXT_LIMIT]


#: pandoc 的**输入格式必须显式给**：实测这台机器上的 pandoc 不认 `-f auto`
#: （那是新版才有的），报的是 `Unknown input format auto` —— 于是转出来全空，
#: 而现象是"文档能打开但一个字都没有"，很难往"版本差"上想。
PANDOC_FROM = {".docx": "docx", ".rtf": "rtf", ".odt": "odt", ".md": "markdown"}


def _run_pandoc(path: Path, to: str) -> str:
    """让 pandoc 把一份文档转成 `to`（`plain` / `html`）。没有它就返回空串。"""
    from_fmt = PANDOC_FROM.get(Path(path).suffix.lower(), "markdown")
    found, _why = toolchain.ensure("pandoc")
    if found is None:
        return ""
    try:
        done = subprocess.run(
            [str(found), "-f", from_fmt, "-t", to, "--wrap=none", str(path)],
            capture_output=True,
            timeout=PDF_TIMEOUT * 3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if done.returncode != 0:
        return ""
    return done.stdout.decode("utf-8", errors="replace")


def slides_of(path: Path) -> list[dict]:
    """把 `.pptx` 解成幻灯片列表：`[{index, title, lines}]`。

    pptx 就是个 zip：幻灯片在 `ppt/slides/slideN.xml`，正文在 `<a:t>` 里。
    **自己解而不是等一个库**：这段是确定性的 XML 遍历，引一个包来读自己机器上
    的一堆 XML 不划算（与资料模块不引 YAML 库是同一条取舍）。
    页面顺序按数字排（`slide2` 要排在 `slide10` 前面 —— 按字符串排会乱）。
    """
    import re as _re
    import zipfile as _zip

    try:
        with _zip.ZipFile(path) as box:
            names = [n for n in box.namelist() if _re.match(r"^ppt/slides/slide\d+\.xml$", n)]
            names.sort(key=lambda n: int(_re.findall(r"\d+", n)[-1]))
            out: list[dict] = []
            for order, name in enumerate(names, start=1):
                xml = box.read(name).decode("utf-8", errors="replace")
                lines = [
                    _re.sub(r"\s+", " ", chunk).strip()
                    for chunk in _re.findall(r"<a:t>(.*?)</a:t>", xml, flags=_re.S)
                ]
                lines = [line for line in lines if line]
                out.append(
                    {
                        "index": order,
                        "title": (lines[0] if lines else f"第 {order} 页")[:120],
                        "lines": lines[1:] if lines else [],
                    }
                )
            return out
    except (OSError, _zip.BadZipFile, KeyError):
        return []


def extract(path: Path, kind: str) -> str:
    """抽正文。抽不出来就返回空串 —— 调用方要接受这一点，不要当成错误。"""
    if kind == "pdf":
        return _extract_pdf(path)
    if kind == "docx":
        return _run_pandoc(path, "plain")[:TEXT_LIMIT]
    if kind == "pptx":
        # 幻灯片之间留个分隔，检索时"这一页"与"那一页"不至于粘成一段
        return "\n\n".join(
            "\n".join([one["title"], *one["lines"]]) for one in slides_of(path)
        )[:TEXT_LIMIT]
    if kind == "legacy":
        return ""      # 老式二进制：照实说抽不到，不硬凑
    if kind != "text":
        return ""
    try:
        raw = path.read_bytes()[: TEXT_LIMIT * 4]
    except OSError:
        return ""
    text = raw.decode("utf-8", errors="replace")
    if "\x00" in text[:2000]:
        return ""  # 二进制被当成文本传上来了
    return text[:TEXT_LIMIT]


#: 从文档里带出来的可执行东西：脚本、事件属性、`javascript:` 地址。
#: 本地文档里嵌一段 `<script>` 是**合法的**（Office 文档允许内嵌 HTML），
#: 但把它注进我们的页面等于让别人的文件在我们的源里跑。
_SCRIPT_TAG = re.compile(r"<\s*script\b.*?</\s*script\s*>", re.IGNORECASE | re.DOTALL)
_ANY_EVENT = re.compile(r"\son[a-z]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.IGNORECASE)
_JS_URL = re.compile(r"(href|src)\s*=\s*(\"|')\s*javascript:[^\"']*\2", re.IGNORECASE)


def sanitize_html(html: str) -> str:
    """把转出来的 HTML 里**能执行的东西**去掉，其余原样留着。

    只做减法、不改结构：脚注、表格、图片、样式都留 —— 它们的价值正是"能读"，
    而去掉的这三类东西与"读懂这份文档"无关。
    """
    out = _SCRIPT_TAG.sub("", html or "")
    out = _ANY_EVENT.sub("", out)
    return _JS_URL.sub("", out)


def capabilities() -> dict:
    """文件处理这一层现在能干什么、缺什么。

    界面与模型都读它：一份文档抽不出文字时，要说得出是**缺哪个组件**，
    而不是让用户面对一片空白猜"是不是这份文件有问题"。
    """
    return toolchain.status()


def to_html(path: Path) -> str:
    """把一份文档转成 HTML（目前只有 docx 这条路走 pandoc）。失败给空串。"""
    return sanitize_html(_run_pandoc(path, "html"))[:TEXT_LIMIT]


def save(db, user, name: str, mime: str, data: bytes) -> dict:  # noqa: ANN001
    """落盘 + 入库 + 抽正文。返回给前端的元数据（不含正文）。

    先判体积再写盘：`MAX_BYTES` 之外的东西根本不该碰到磁盘。
    """
    if not data:
        return {"error": "空文件？"}
    if len(data) > MAX_BYTES:
        return {"error": f"文件太大（{len(data) // 1024 // 1024}MB，上限 {MAX_BYTES // 1024 // 1024}MB）"}

    safe_name = (name or "附件").replace("/", "_").replace("\\", "_")[:200]
    kind = kind_of(safe_name, mime)
    digest = hashlib.sha256(data).hexdigest()

    directory = uploads_dir() / str(user.id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (digest[:16] + Path(safe_name).suffix.lower()[:12])
    path.write_bytes(data)

    row = Attachment(
        user_id=user.id,
        name=safe_name,
        mime=(mime or "")[:120],
        size=len(data),
        sha256=digest,
        # 相对 api/ 的路径 —— 换机器也能搬（绝对路径不进库）
        path=str(path.relative_to(Path(__file__).resolve().parent.parent)),
        kind=kind,
        text=extract(path, kind),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {
        "id": str(row.id),
        "name": row.name,
        "mime": row.mime,
        "size": row.size,
        "kind": row.kind,
        "textChars": len(row.text),
        "preview": row.text[:400],
    }


def path_of(attachment: Attachment) -> Path:
    """库里的相对路径 → 真实文件。"""
    return Path(__file__).resolve().parent.parent / attachment.path
