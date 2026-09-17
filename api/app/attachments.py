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
import subprocess
from pathlib import Path

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


def uploads_dir() -> Path:
    """附件根目录（相对 `api/`）—— 已在 `.gitignore` 里，绝不进版本库。"""
    return Path(__file__).resolve().parent.parent / "var" / "uploads"


def kind_of(name: str, mime: str) -> str:
    suffix = Path(name or "").suffix.lower()
    if suffix == ".pdf" or "pdf" in (mime or ""):
        return "pdf"
    if suffix in IMAGE_SUFFIXES or (mime or "").startswith("image/"):
        return "image"
    if suffix in TEXT_SUFFIXES or (mime or "").startswith("text/"):
        return "text"
    # 认不出后缀但像纯文本的（json/csv 之类常被浏览器报成别的）也当文本试一把
    return "other"


def _extract_pdf(path: Path) -> str:
    """`pdftotext` 抽 PDF 正文。系统里没有它就直接放弃（不报错）。"""
    try:
        done = subprocess.run(
            ["pdftotext", "-layout", "-q", str(path), "-"],
            capture_output=True,
            timeout=PDF_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if done.returncode != 0:
        return ""
    return done.stdout.decode("utf-8", errors="replace")[:TEXT_LIMIT]


def extract(path: Path, kind: str) -> str:
    """抽正文。抽不出来就返回空串 —— 调用方要接受这一点，不要当成错误。"""
    if kind == "pdf":
        return _extract_pdf(path)
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
