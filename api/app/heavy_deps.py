"""重型运行组件的**本机缓存**：第一次打开时取一次，之后离线可用。

## 为什么要单独一层

「从哪拿运行时」与「怎么用运行时」是两件事：

* 前者有位置（缓存在哪）、校验（哈希对不对）、失败处理（没网怎么办）；
* 后者只关心"给我一个能加载的 base URL"。

散在 `tools.py` 里的时候，这三件事挤在 `pyodide_base()` 一个函数里，
而它当时只会做一件事：看看本机有没有副本、没有就退回 CDN。

## 为什么是"缓存"而不是"打进包里"

Pyodide 是 **146M**（`vendor/pyodide/` 实测），而整个应用其余部分加起来约 5M。
把它塞进安装包，等于为了"对话里跑一段 Python"这个可选能力让每次分发都背上 146M。
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

## Android：清单必须随包

手机上没有 `vendor/`（146M 不进包，与桌面同一条取舍），所以它**只能**走第 3 步
（按清单下载）。但这要求清单本身在手机上 —— 而 Chaquopy 把 Python 源码编进
`app.imy`，`__file__` 的层级与仓库不同，按 `ROOT/build/` 推是找不到的。
所以 Android 的打包流程把这份 3KB 的清单放进 `assets/runtime/`，启动器（`BootHelper`）
把它拷到数据目录、`android_boot` 设 `QF_HEAVY_MANIFEST` 指过去（见 `manifest_path()`）。
少了这一步，"用到时取一次"在手机上就是一句永远不兑现的承诺。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import threading
import time
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

#: Android 上清单**不跟 `ROOT` 走**：Chaquopy 把 Python 源码编进 `app.imy`，
#: `__file__` 的层级与仓库里不同（多一层），按 `parents[2]` 推出来的是
#: `files/chaquopy/`，而清单被解在 `files/chaquopy/app.imy/` 里。启动器会把
#: assets 里那份（3KB，随包）拷到数据目录，并用这个变量指过去。
#:
#: 为什么非要有它：清单是**校验**的依据。手机上它缺失时 `_download()` 会拒绝
#: 下载（"没有清单文件，不敢下载"），于是"用到时取一次"在手机上永远不成立 ——
#: 这正是 2026-11-09 之前的状态。
MANIFEST_ENV = "QF_HEAVY_MANIFEST"

#: 随包的"补丁"目录：清单里有、但 CDN 上拿不到的文件放这儿（例如 quizforge
#: 自带的 `schemdraw-0.23-...whl` —— pyodide 0.26.4 的发行版里没有这一版，
#: 实测 404）。首启先从这个目录拷，剩下的才联网下。
BUNDLE_ENV = "QF_HEAVY_BUNDLE"

#: `pyodide.js` 是入口：没有它这份副本就是没用的
ENTRY = "pyodide.js"

#: 失败后多久内不再自动重试。**不是省流量**：没有这道退避时，用户每问一次
#: "跑一下这段"，`run_python` 都会重新起一轮下载、再报一次"已经在后台取了"
#: —— 没网的时候表现就是永远在取。过了这个窗口再试（换网、开代理之后自然恢复）。
FAIL_BACKOFF_SECONDS = 600

_lock = threading.Lock()

#: 最近一次取用的结果：`{"detail": 失败原因, "at": time.monotonic()}`。
#: 给 `run_python` 用 —— 「在取」与「取不到」必须能分开说。
_failure: dict[str, object] = {"detail": "", "at": 0.0}

#: 下载进度：给**首次安装的启动页**画进度条用（用户 2026-11-10："大的依赖还是
#: 不该让它在背后自己下，在首次安装的时候显示进度条，表示正在下载必要的依赖中"）。
#: 键名与 `/api/heavy/status` 的响应一致：`done`/`total` 是**字节**，
#: `files`/`filesTotal` 是文件个数，进度条拿 done/total 算百分比。
_progress: dict[str, object] = {
    "state": "idle",  # idle | running | ok | failed
    "done": 0,
    "total": 0,
    "files": 0,
    "filesTotal": 0,
    "detail": "",
}


def _mark(**fields: object) -> None:
    """更新进度。不加锁：单线程写、HTTP 只读，CPython 的 dict.update 足够原子。"""
    _progress.update(fields)


def progress() -> dict:
    """进度快照（`/api/heavy/status` 原样返回它）。

    `percent` 按**文件数**算，不按字节：清单里的 `bytes` 是打包时统计的近似值
    （实测 152M，真下 171M），按它画条会先跑满再卡住。文件数单调、不撒谎。
    """
    snapshot = dict(_progress)
    files = int(snapshot.get("files") or 0)
    files_total = int(snapshot.get("filesTotal") or 0)
    snapshot["percent"] = int(files * 100 / files_total) if files_total else 0
    return snapshot


def cache_dir() -> Path:
    """缓存落在应用数据目录下（跟着用户的库走，能被一起备份/搬走）。"""
    return get_settings().data_dir / "cache" / "pyodide"


def manifest_path() -> Path:
    """清单实际在哪：环境变量优先（Android），否则用随仓库的默认位置。"""
    override = os.environ.get(MANIFEST_ENV, "").strip()
    return Path(override) if override else MANIFEST_FILE


def _manifest_payload() -> dict:
    """整份清单（`files` + `bytes`）。读不动就当空。"""
    path = manifest_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def manifest() -> dict[str, str]:
    """`文件名 → sha256`。清单不在（比如没打全的检出）就返回空字典。"""
    data = _manifest_payload()
    files = data.get("files") if isinstance(data, dict) else None
    return {str(k): str(v) for k, v in (files or {}).items()}


def manifest_total() -> int:
    """清单里所有文件的总字节数（进度条的分母）。"""
    raw = _manifest_payload().get("bytes")
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


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


def verify(directory: Path | None = None) -> dict:
    """逐个文件核对哈希，回答**缺了什么**（`is_ready()` 只回答"行不行"）。

    设置页那颗「重新下载并校验」按下去要能把话说清楚：是齐的、还是缺三个、
    缺的是哪几个。判据只有哈希 —— 文件在、名字对、大小对都不算数
    （下载被打断时正是这种"看起来有"的状态）。用户 2026-11-10："如果安装的时候
    没有下载好或者出问题，那么可以通过这个按钮重新下载并校验资源完整性。"
    """
    target = directory or cache_dir()
    expected = manifest()
    missing: list[str] = []
    if expected:
        for name, want in sorted(expected.items()):
            path = target / name
            if not path.is_file() or _sha256(path) != want:
                missing.append(name)
    elif not (target / ENTRY).is_file():
        missing.append(ENTRY)

    total = len(expected) if expected else (0 if not missing else 1)
    if not expected:
        detail = "没有校验清单，只核对了入口文件"
    elif missing:
        detail = f"{len(missing)} 个文件缺失或损坏"
    else:
        detail = "全部文件与清单一致"
    return {
        "ready": not missing,
        "checked": max(0, total - len(missing)),
        "total": total,
        "manifest": len(expected),
        "missingCount": len(missing),
        # 只带前几个名字：清单有上百个文件，全列出来会把响应和界面都撑坏
        "missing": missing[:8],
        "detail": detail,
        "cache": str(target),
    }


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
        _mark(state="failed", detail="没有校验清单，取不到运行时")
        return False
    target = cache_dir()
    target.mkdir(parents=True, exist_ok=True)
    total_bytes = manifest_total()
    done_bytes = 0
    _mark(
        state="running",
        done=0,
        total=total_bytes,
        files=0,
        filesTotal=len(expected),
        detail="正在下载必要的依赖",
    )
    # 先从随包目录补齐 CDN 拿不到的那几个（校验同一份哈希，不做例外）
    bundled = Path(os.environ.get(BUNDLE_ENV, "").strip()) if os.environ.get(BUNDLE_ENV) else None
    if bundled and bundled.is_dir():
        for name, want in expected.items():
            source = bundled / name
            destination = target / name
            if not source.is_file():
                continue
            if destination.is_file() and _sha256(destination) == want:
                continue
            if _sha256(source) == want:
                shutil.copy2(source, destination)
                log(f"运行时：从随包副本拷入 {name}")
    for index, (name, want) in enumerate(sorted(expected.items()), start=1):
        destination = target / name
        if destination.is_file() and _sha256(destination) == want:
            done_bytes += destination.stat().st_size
            _mark(done=done_bytes, files=index)
            continue
        url = CDN_BASE + name
        log(f"运行时：下载 {name}（{index}/{len(expected)}）")
        temporary = destination.with_suffix(destination.suffix + ".part")
        try:
            with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310
                with temporary.open("wb") as handle:
                    # 自己读块而不是 copyfileobj：进度条要的是**过程中的字节数**
                    while True:
                        chunk = response.read(256 * 1024)
                        if not chunk:
                            break
                        handle.write(chunk)
                        done_bytes += len(chunk)
                        _mark(done=done_bytes)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            temporary.unlink(missing_ok=True)
            message = f"下载失败（{name}）—— {exc}"
            log(f"运行时：{message}")
            _mark(state="failed", detail=message)
            return False
        got = _sha256(temporary)
        if got != want:
            temporary.unlink(missing_ok=True)
            message = f"{name} 校验不过（期望 {want[:12]}… 实际 {got[:12]}…）"
            log(f"运行时：{message}")
            _mark(state="failed", detail=message)
            return False
        temporary.replace(destination)
        done_bytes += destination.stat().st_size
        _mark(files=index)
    log(f"运行时：下载完成（{done_bytes / 1024 / 1024:.1f} MB）→ {target}")
    _mark(state="ok", detail="依赖已就绪")
    return True


def _remember(ok: bool, detail: str) -> None:
    """记下这次取用的结果（成功就清掉失败状态）。"""
    _failure["detail"] = "" if ok else detail
    _failure["at"] = 0.0 if ok else time.monotonic()


def last_failure() -> str:
    """最近一次取用失败的原因；成功后是空串。"""
    return str(_failure["detail"] or "")


def prefetching() -> bool:
    """后台是不是还有一轮在取（区分"在取"与"取不到"用）。"""
    return _prefetching


def ensure(*, allow_download: bool = True, log: Callable[[str], None] | None = None) -> tuple[bool, str]:
    """把运行时准备好。**幂等**，多线程同时调用只会跑一次。

    返回 `(是否可用, 一句人话的说明)` —— 失败时这句话要能直接讲给用户听。
    """
    notes: list[str] = []

    def say(message: str) -> None:
        notes.append(message)
        if log is not None:
            log(message)

    with _lock:
        if is_ready():
            _remember(True, "")
            _mark(state="ok", detail="本地已有运行时")
            return True, "本地已有运行时"

        if _copy_from_vendor(say):
            bad = _missing_in(cache_dir(), manifest())
            if not bad:
                _remember(True, "")
                _mark(state="ok", detail="从仓库 vendor/ 拷入（未联网）")
                return True, "从仓库 vendor/ 拷入（未联网）"
            say(f"运行时：vendor 副本校验不过（{', '.join(bad[:3])}）")

        if not allow_download:
            _remember(False, "离线且没有本地副本")
            _mark(state="failed", detail="离线且没有本地副本")
            return False, "离线且没有本地副本 —— 跑 Python 这一项暂时不可用"

        if _download(say):
            _remember(True, "")
            return True, "已下载到本机缓存"

        # 失败原因取最后一条日志（"没有清单文件""下载失败（x）—— …"）：那比
        # "取不到"四个字有用得多，而且它就是 `run_python` 接下来要讲给用户的那句。
        reason = notes[-1] if notes else "取不到运行时（没网或下载失败）"
        _remember(False, reason)
        _mark(state="failed", detail=reason)
        return False, f"{reason} —— 跑 Python 这一项暂时不可用"


_prefetching = False


def start_prefetch(log: Callable[[str], None] | None = None, *, force: bool = False) -> bool:
    """在**后台线程**里把运行时准备好（已经在准备或已就绪就返回 False）。

    为什么不就地同步下载：Pyodide 是 146M，同步下载会把这次工具调用挂住几十秒
    到几分钟 —— 界面上看起来就是卡死。所以"准备好"这件事放到后台，
    调用方只需要如实告诉用户"正在取"。

    `force=True` 跳过失败退避（设置页里"重试"用）。默认退避见 `FAIL_BACKOFF_SECONDS`。
    """
    global _prefetching
    with _lock:
        if is_ready():
            # 已经在本机（升级上来的老用户）—— 启动页看到 ok 就直接放行
            _mark(state="ok", detail="本地已有运行时")
            return False
        if _prefetching:
            return False
        failed_at = float(_failure["at"] or 0.0)
        if not force and failed_at and (time.monotonic() - failed_at) < FAIL_BACKOFF_SECONDS:
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


def repair(log: Callable[[str], None] | None = None) -> dict:
    """设置页那颗「重新下载并校验」：先全量校验，缺什么补什么。**幂等**。

    为什么不直接 `start_prefetch(force=True)`：缓存完整时它只返回 False，
    调用方分不清"不用修"与"没起来"，而用户按这个按钮要的正是那句结论
    （用户 2026-11-10："安装的时候没有下载好或者出问题，可以通过这个按钮
    重新下载并校验资源完整性"）。所以先 `verify()` 拿到明细，再决定是否起下载。

    返回 `verify()` 的字段，外加 `started`（这轮真的起了后台下载吗）与
    `state`（`ok`/`running`/`failed`，前端据此决定要不要轮询 `/api/heavy/status`）。
    """
    report = verify()
    if report["ready"]:
        _mark(state="ok", detail="本地已有运行时")
        return {**report, "started": False, "state": "ok"}

    if prefetching():
        return {**report, "started": False, "state": "running"}

    if not report["manifest"]:
        # 没有清单就不能下（下了也校验不了）—— 如实说，别起一轮注定失败的下载
        detail = "没有校验清单，不敢下载（无法校验）"
        _mark(state="failed", detail=detail)
        return {**report, "started": False, "state": "failed", "detail": detail}

    # 先把"校验中发现的那几个"算进进度起点：界面上不该从 0/152 重新数，
    # 因为绝大多数文件其实是对的，只补缺的那几个。
    _mark(
        state="running",
        done=0,
        total=manifest_total(),
        files=report["checked"],
        filesTotal=report["total"],
        detail="正在校验并补齐依赖",
    )
    started = start_prefetch(log=log, force=True)
    if not started and not prefetching():
        # 竞态兜底：判完不完整到这里，另一轮可能刚好收尾（或已经就绪）
        now = verify()
        _mark(state="ok" if now["ready"] else "failed", detail=now["detail"])
        return {**now, "started": False, "state": "ok" if now["ready"] else "failed"}
    return {**report, "started": started, "state": "running"}


def base_url() -> str | None:
    """运行时挂在哪。给前端用的路径；没有缓存就返回 None。

    相对路径前要加 `__ORIGIN__` 占位符：沙箱页面（`srcdoc`）里**相对路径解析不了**
    （它的 base 是 `about:srcdoc`），而沙箱里 `location.origin` 是不透明的，
    只有宿主知道自己的 origin，所以由宿主替换。
    """
    return "__ORIGIN__/assets/pyodide/" if is_ready() else None
