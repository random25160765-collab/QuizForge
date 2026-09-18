"""重型运行组件的**本机缓存**：第一次打开时取一次，之后离线可用。

## 为什么要单独一层

「从哪拿运行时」与「怎么用运行时」是两件事：

* 前者有位置（缓存在哪）、校验（哈希对不对）、失败处理（没网怎么办）；
* 后者只关心"给我一个能加载的 base URL"。

散在 `tools.py` 里的时候，这三件事挤在 `pyodide_base()` 一个函数里，
而它当时只会做一件事：看看本机有没有副本、没有就退回 CDN。

## 为什么是"缓存"而不是"打进包里"

Pyodide 是 **76M**（`vendor/pyodide/` 实测），而整个应用其余部分加起来约 5M。
把它塞进安装包，等于为了"对话里跑一段 Python"这个可选能力让每次分发都背上 76M。
所以（用户定的取舍）：

* **分发包保持小体积** —— 里面没有 Pyodide；
* **第一次打开时取一次**，落进 `data/cache/pyodide/`，**逐个文件校验 sha256**；
* **仓库里 `vendor/` 仍然自包含** —— 开发时直接从它拷进缓存，**不联网**；
* 取不到也不影响别的能力：`run_python` 会明确说"运行时没准备好"，而不是白屏。

## 顺序

1. 缓存里已经齐了 → 直接用；
2. `vendor/pyodide/` 在（开发机 / 离线开发）→ 拷进缓存并校验，**不联网**；
3. 有清单且允许联网 → 从钉死版本的 CDN 逐个下载并校验；
4. 都没有 → 返回失败原因，调用方把它讲给用户听。

清单在 `build/pyodide-manifest.json`（`文件名 → sha256`），由
`python3 build/package.py manifest` 从本机 `vendor/pyodide/` 生成并提交进仓库 ——
于是"下载的东西对不对"这件事**在离线时也能判断**。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

from .config import get_settings

#: 版本钉死：CDN 上的东西会变，而这份运行时会被长期缓存在用户机器上
PYODIDE_VERSION = "0.26.4"
CDN_BASE = f"https://cdn.jsdelivr.net/pyodide/v{PYODIDE_VERSION}/full/"


def _root() -> Path:
    """资源根：开发时是仓库根，打包后是解包目录。

    PyInstaller 单文件模式把东西解到临时目录（`sys._MEIPASS`），`__file__` 就在那儿 ——
    清单（`build/pyodide-manifest.json`）跟着包走，所以要找的是**那个**根，
    而不是"`__file__` 往上数几级"。不认这个区别的后果很隐蔽：
    打包后每次下载都校验不了（清单找不到），于是"首启下载"变成"每次都下"。
    """
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    return Path(__file__).resolve().parents[2]


ROOT = _root()
VENDOR_DIR = ROOT / "vendor" / "pyodide"
MANIFEST_FILE = ROOT / "build" / "pyodide-manifest.json"

#: `pyodide.js` 是入口：没有它这份副本就是没用的
ENTRY = "pyodide.js"

_lock = threading.Lock()


def cache_dir() -> Path:
    """缓存落在应用数据目录下（跟着用户的库走，能被一起备份/搬走）。"""
    return get_settings().data_dir / "cache" / "pyodide"


def manifest() -> dict[str, str]:
    """`文件名 → sha256`。清单不在（比如没打全的检出）就返回空字典。"""
    if not MANIFEST_FILE.is_file():
        return {}
    try:
        data = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    files = data.get("files") if isinstance(data, dict) else None
    return {str(k): str(v) for k, v in (files or {}).items()}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _missing_in(directory: Path, expected: dict[str, str]) -> list[str]:
    """目录里缺了哪些文件 / 哪些对不上哈希。

    **先看文件在不在再算哈希**：下载被打断时缓存里就是"有一半"的状态，
    而 `_sha256` 对着不存在的文件会抛 `FileNotFoundError` —— 那会让
    `is_ready()` 从"返回 False"变成"炸掉"，恰好是最常见的那个场景。
    """
    if not (directory / ENTRY).is_file():
        return [ENTRY]
    if not expected:
        # 没有清单时只认"入口在不在"（vendor 副本就是这种情况）
        return []
    missing = [
        name
        for name, want in expected.items()
        if not (directory / name).is_file() or _sha256(directory / name) != want
    ]
    return missing


def is_ready(directory: Path | None = None) -> bool:
    target = directory or cache_dir()
    return bool((target / ENTRY).is_file() and not _missing_in(target, manifest()))


def file_path(name: str) -> Path | None:
    """按名字取缓存里的文件（**只认清单里的名字**，顺带挡住路径穿越）。"""
    expected = manifest()
    if not name or "/" in name or "\\" in name or ".." in name:
        return None
    if expected and name not in expected:
        return None
    path = cache_dir() / name
    return path if path.is_file() else None


def _copy_from_vendor(log: Callable[[str], None]) -> bool:
    if not (VENDOR_DIR / ENTRY).is_file():
        return False
    target = cache_dir()
    target.mkdir(parents=True, exist_ok=True)
    for src in sorted(VENDOR_DIR.iterdir()):
        if src.is_file() and src.name != "SOURCE.md":
            shutil.copy2(src, target / src.name)
    log(f"运行时：从仓库 vendor/ 拷入（未联网）→ {target}")
    return True


def _download(log: Callable[[str], None]) -> bool:
    expected = manifest()
    if not expected:
        log("运行时：没有清单文件，不敢下载（无法校验）")
        return False
    target = cache_dir()
    target.mkdir(parents=True, exist_ok=True)
    total_bytes = 0
    for index, (name, want) in enumerate(sorted(expected.items()), start=1):
        destination = target / name
        if destination.is_file() and _sha256(destination) == want:
            total_bytes += destination.stat().st_size
            continue
        url = CDN_BASE + name
        log(f"运行时：下载 {name}（{index}/{len(expected)}）")
        temporary = destination.with_suffix(destination.suffix + ".part")
        try:
            with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310
                with temporary.open("wb") as handle:
                    shutil.copyfileobj(response, handle)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            temporary.unlink(missing_ok=True)
            log(f"运行时：下载失败（{name}）—— {exc}")
            return False
        got = _sha256(temporary)
        if got != want:
            temporary.unlink(missing_ok=True)
            log(f"运行时：{name} 校验不过（期望 {want[:12]}… 实际 {got[:12]}…）")
            return False
        temporary.replace(destination)
        total_bytes += destination.stat().st_size
    log(f"运行时：下载完成（{total_bytes / 1024 / 1024:.1f} MB）→ {target}")
    return True


def ensure(*, allow_download: bool = True, log: Callable[[str], None] | None = None) -> tuple[bool, str]:
    """把运行时准备好。**幂等**，多线程同时调用只会跑一次。

    返回 `(是否可用, 一句人话的说明)` —— 失败时这句话要能直接讲给用户听。
    """
    say = log or (lambda _message: None)
    with _lock:
        if is_ready():
            return True, "本地已有运行时"

        if _copy_from_vendor(say):
            bad = _missing_in(cache_dir(), manifest())
            if not bad:
                return True, "从仓库 vendor/ 拷入（未联网）"
            say(f"运行时：vendor 副本校验不过（{', '.join(bad[:3])}）")

        if not allow_download:
            return False, "离线且没有本地副本 —— 跑 Python 这一项暂时不可用"

        if _download(say):
            return True, "已下载到本机缓存"

        return False, "取不到运行时（没网或下载失败）—— 跑 Python 这一项暂时不可用"


_prefetching = False


def start_prefetch(log: Callable[[str], None] | None = None) -> bool:
    """在**后台线程**里把运行时准备好（已经在准备或已就绪就返回 False）。

    为什么不就地同步下载：Pyodide 是 76M，同步下载会把这次工具调用挂住几十秒
    到几分钟 —— 界面上看起来就是卡死。所以"准备好"这件事放到后台，
    调用方只需要如实告诉用户"正在取"。
    """
    global _prefetching
    with _lock:
        if _prefetching or is_ready():
            return False
        _prefetching = True

    def _work() -> None:
        global _prefetching
        try:
            ok, detail = ensure(log=log)
            if log:
                log(f"运行时：{'就绪' if ok else '没有就绪'} —— {detail}")
        finally:
            with _lock:
                _prefetching = False

    threading.Thread(target=_work, name="qf-heavy-prefetch", daemon=True).start()
    return True


def base_url() -> str | None:
    """运行时挂在哪。给前端用的路径；没有缓存就返回 None。

    相对路径前要加 `__ORIGIN__` 占位符：沙箱页面（`srcdoc`）里**相对路径解析不了**
    （它的 base 是 `about:srcdoc`），而沙箱里 `location.origin` 是不透明的，
    只有宿主知道自己的 origin，所以由宿主替换。
    """
    return "__ORIGIN__/assets/pyodide/" if is_ready() else None
