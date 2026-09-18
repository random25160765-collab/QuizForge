"""外部命令行工具的取用与自举 —— 文件处理是**我们的依赖**，不是这台机器的运气。

为什么要有这一层：PDF 抽正文要 `pdftotext`、docx 转 HTML 要 `pandoc`。
先前是"直接用名字调"，于是换一台机器（或者用户那台）就**静默抽不出文字** ——
现象是"文档能打开但一个字都没有"，而真正的原因藏在 PATH 里。

取用顺序四级（越靠前越便宜）：

1. **系统 PATH** —— 已经装了就用它，不折腾（开发机与 Linux 发行版多半有）
2. **`vendor/tools/<组件>/`** —— 仓库自包含（开发与离线开发可用，与 pyodide 同一套）
3. **`data/cache/tools/<组件>/`** —— 首启下载落在本机缓存，之后离线可用
4. **首启下载** —— 只见 `build/toolchain.json` 里钉过 URL 与 sha256 的包

一条硬规矩：**没有钉 sha256 的包一律不装**。装一个没校验的可执行文件，
比"这个功能暂时用不了"糟得多 —— 所以宁可报"配置里还没钉哈希"，也不猜。

钉哈希的活由 `tools/pin_toolchain.py` 干（有网的时候跑一次，把 URL 与 sha256 写进清单）。
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import stat
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

#: 清单：`{组件: {平台: {url, sha256, member}}}`。与 pyodide 那份清单同一个位置、同一个道理。
MANIFEST_FILE = Path(__file__).resolve().parents[2] / "build" / "toolchain.json"

#: 仓库自包含的那一份（开发与离线开发用）。分发包里可能没有，所以它是可选的。
VENDOR_DIR = Path(__file__).resolve().parents[2] / "vendor" / "tools"

DOWNLOAD_TIMEOUT = 180  # 秒：几十 MB 的包，慢网也够


def _config():
    from .config import get_settings

    return get_settings()


def cache_dir() -> Path:
    """本机缓存（与应用数据在一起，卸载应用就等于清干净）。"""
    return Path(_config().data_dir) / "cache" / "tools"


#: 组件表：每个组件提供哪个可执行文件、干什么用。
#: `why` 是给界面与模型看的一句话 —— 缺组件时要说得出"缺的是干什么的"。
COMPONENTS: dict[str, dict[str, Any]] = {
    "pdftotext": {
        "why": "抽 PDF 的正文（poppler 里的那个命令）",
        "binary": "pdftotext",
    },
    "pandoc": {
        "why": "把 docx / rtf / odt 转成 HTML 与纯文本",
        "binary": "pandoc",
    },
}


def platform_key() -> str:
    """`linux-x64` / `win-x64` / `mac-arm64` 这种。清单按它分平台。"""
    system = {"linux": "linux", "win32": "win", "darwin": "mac"}.get(sys.platform, sys.platform)
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
    return f"{system}-{arch}"


def manifest() -> dict[str, dict[str, dict[str, str]]]:
    """读清单。不在（比如没钉过）就给空字典 —— 那等于"全都还没配"。"""
    try:
        raw = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _in_dir(directory: Path, name: str) -> Path | None:
    """在某个目录里找可执行文件（含 `bin/` 一层，解出来的包多是这样）。"""
    for probe in (directory / name, directory / "bin" / name, directory / f"{name}.exe"):
        if probe.is_file():
            return probe
    return None


def _cached(name: str) -> Path | None:
    spec = COMPONENTS.get(name) or {}
    binary = str(spec.get("binary") or name)
    folder = cache_dir() / name
    if not folder.is_dir():
        return None
    found = _in_dir(folder, binary)
    if found:
        return found
    # 解出来带一层版本目录是常态（`pandoc-3.7/bin/pandoc`），往里找一层
    for child in sorted(folder.iterdir()):
        if child.is_dir():
            found = _in_dir(child, binary)
            if found:
                return found
    return None


def _vendored(name: str) -> Path | None:
    return _in_dir(VENDOR_DIR / name, str((COMPONENTS.get(name) or {}).get("binary") or name))


def resolve(name: str) -> tuple[Path | None, str]:
    """找一个可执行文件：`(路径, 来源)`。找不到时路径为 None、来源是原因。

    顺序见模块开头那张表：系统 → vendor → 缓存。
    （**下载不在这里做** —— `resolve` 是"现在有没有"，`ensure` 才是"去弄一个来"。）
    """
    spec = COMPONENTS.get(name)
    if spec is None:
        return None, f"不认识的组件：{name}"
    binary = str(spec.get("binary") or name)
    system = shutil.which(binary)
    if system:
        return Path(system), "系统"
    vendored = _vendored(name)
    if vendored:
        return vendored, "随仓库自带"
    cached = _cached(name)
    if cached:
        return cached, "本机缓存"
    pinned = (manifest().get(name) or {}).get(platform_key()) or {}
    if not pinned.get("url"):
        return None, f"这台机器上没有 {binary}，清单里也还没钉它（跑 `python -m tools.pin_toolchain {name}` 钉一次）"
    if not pinned.get("sha256"):
        return None, f"清单里 {binary} 只有地址没有 sha256 —— 没校验的包不装"
    return None, f"本机没有 {binary}（清单里钉好了，用的时候会取一次）"


def _fetch(url: str) -> Path:
    """下载到一个临时文件（**流式**：几十 MB 不该整个读进内存）。"""
    handle, raw = tempfile.mkstemp(prefix="qf-tool-")
    os.close(handle)
    target = Path(raw)
    try:
        with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT) as response:  # noqa: S310
            with target.open("wb") as out:
                shutil.copyfileobj(response, out)
    except (urllib.error.URLError, OSError, TimeoutError):
        target.unlink(missing_ok=True)
        raise
    return target


def _unpack(archive: Path, into: Path) -> None:
    """解开 tar.gz / zip。**只解出文件，不保留可执行位之外的属性**。"""
    into.mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as box:
            box.extractall(into)  # noqa: S202 - 来源是我们自己钉过哈希的包
        return
    with tarfile.open(archive, "r:*") as box:
        box.extractall(into)  # noqa: S202 - 同上


def ensure(name: str, *, log=None) -> tuple[Path | None, str]:
    """确保这个组件能用：没有就去取一次（**只在钉过哈希时才动手**）。

    返回 `(路径, 说明)`。取不动也**不抛** —— 文件处理那边要把它变成一句
    "这份文档抽不出文字，因为缺 X"，而不是一个 500。
    """
    found, why = resolve(name)
    if found:
        return found, why

    spec = COMPONENTS.get(name) or {}
    binary = str(spec.get("binary") or name)
    pinned = (manifest().get(name) or {}).get(platform_key()) or {}
    url, sha = str(pinned.get("url") or ""), str(pinned.get("sha256") or "")
    if not url or not sha:
        return None, why

    folder = cache_dir() / name
    archive: Path | None = None
    try:
        if log:
            log(f"[工具] 取 {binary}：{url}")
        archive = _fetch(url)
        got = _sha256(archive)
        if got != sha:
            return None, f"{binary} 下下来哈希对不上（期望 {sha[:12]}…，得到 {got[:12]}…），已丢弃"
        _unpack(archive, folder)
    except (urllib.error.URLError, OSError, TimeoutError, tarfile.TarError, zipfile.BadZipFile) as exc:
        return None, f"取 {binary} 失败：{type(exc).__name__}"
    finally:
        if archive is not None:
            archive.unlink(missing_ok=True)

    found = _cached(name)
    if not found:
        return None, f"{binary} 解出来了但没找到可执行文件（包的结构与预期不同）"
    found.chmod(found.stat().st_mode | stat.S_IXUSR)   # zip 不带可执行位
    return found, "刚取到（本机缓存）"


def status() -> dict[str, Any]:
    """每个组件的现状。界面与模型都读它 —— "抽不出文字"要能说清是缺哪一环。"""
    out = []
    for key, spec in COMPONENTS.items():
        path, why = resolve(key)
        pinned = (manifest().get(key) or {}).get(platform_key()) or {}
        out.append(
            {
                "name": key,
                "binary": str(spec.get("binary") or key),
                "why": str(spec.get("why") or ""),
                "available": path is not None,
                "path": str(path) if path else "",
                "source": why,
                "pinned": bool(pinned.get("url") and pinned.get("sha256")),
                "platform": platform_key(),
            }
        )
    return {"platform": platform_key(), "components": out}


def prefetch(log=None) -> list[str]:
    """启动时在后台把**钉过哈希**的组件取一次（"首启下载"落在这）。

    只取钉过哈希的；没钉的一律跳过（不猜、不试）。
    """
    done: list[str] = []
    for key in COMPONENTS:
        path, _why = resolve(key)
        if path:
            continue
        pinned = (manifest().get(key) or {}).get(platform_key()) or {}
        if not (pinned.get("url") and pinned.get("sha256")):
            continue
        found, why = ensure(key, log=log)
        if found and log:
            log(f"[工具] {key} 就绪：{why}")
            done.append(key)
    return done


__all__ = ["COMPONENTS", "cache_dir", "ensure", "manifest", "platform_key", "prefetch", "resolve", "status"]
