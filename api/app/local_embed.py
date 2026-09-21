"""本地向量小模型：**首启下载、缓存在本机、之后离线可用**。

## 为什么走本地（而不是调远程）

用户 2026-09-21 的决定：远程按次计费太贵。而这件事的账单不是一次性的 ——
**每次检索都要算一次"查询向量"**，按次计费是随使用量长出来的成本。
本地是一次性下载 + 零边际成本，代价是占一点磁盘。

## 为什么不打进包里

与 Pyodide 同一条规矩（见 `heavy_deps.py`）：分发包保持小体积（exe 26.6MB），
而这个能力是**可选**的。第一次要用时取一次、逐个校验 sha256；取不到不影响别的能力
（`search_material` 会退回纯字面并说明，不是白屏）。

## 拿什么跑（都是实测体积）

* **onnxruntime** 22.5MB + **numpy** 15.9MB（+ protobuf/packaging 0.4MB）——
  **不是 torch / transformers**：那两个会把几百 MB 拖进来，
  而 2026-09-16 那条「不引任何推理框架」的决定针对的正是它们。
* **tokenizers** 3.2MB —— Rust 编的，**能脱离 huggingface_hub 那堆传递依赖单跑**
  （实测：`--no-deps` 取下来解包就能 import）。
* **模型**：`Xenova/bge-m3` 的 int8 ONNX（542MB）。选它的理由是**实测的**：
  短文本考卷 top1 8/8、区分度 0.075（对比 e5-small 的 6/8、0.018），
  而在**真实语料**上这是决定性的 —— e5-small 即使配了窗口化，中文问句仍然返回「前言」。
  体积更大，用户已确认可以接受。

合计约 **600MB** 一次下载（Linux 侧实测 171MB 是上一版 e5-small 的数字），之后零网络。

## CPU 而不是 GPU（实测过，不是没试）

本机有 RTX 5060，但 **GPU 比 CPU 还慢**（每片 57ms vs 45ms）：这个 int8 模型的算子
没有 CUDA 核，ORT 在**每一层**里来回搬张量（实测 24 层 × 4 处 = 336 个 Memcpy），
搬的比算的多。另外 `onnxruntime-gpu` 是 235MB（CPU 版 22.5MB 的 10 倍）、
还要用户机器上有匹配的 cuDNN —— 对"双击即用"不划算。所以固定走 CPU EP。

## 一条容易漏的坑：前缀

e5 家族是**带前缀训练**的（正文 `passage: `、查询 `query: `），漏了不报错、
只是明显搜得差 —— 那种错最难查。**bge-m3 不需要前缀**（加了反而掉点）。
所以前缀由本模块按 `PREFIX` 表加，`embed()` 收 `kind` 而不是让调用方自己拼：
这种约定放在调用方，一定会有人忘。
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import struct
import sys
import threading
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from pathlib import Path

from .config import get_settings

#: 清单（`文件名 → {sha256, url, platform, python, kind}`）。由
#: `python3 build/embed_manifest.py` 生成并提交 —— 清单进版本库才能**离线判断**
#: "下载到的东西对不对"（与 Pyodide 清单同一个理由）。
MANIFEST_FILE = "embed-manifest.json"

#: 查询/正文的前缀。**bge-m3 两个都用空串**（它是无前缀训练的 —— 加了反而掉点）。
#: 这个机制留着是因为模型是可换的：e5 家族就**必须**带 `query: ` / `passage: `，
#: 换回去时只改这张表，不用去翻调用方。
PREFIX = {"query": "", "passage": ""}

#: 单条文本截到多少 token。
#:
#: **bge-m3 的原生上限是 8192，但那个数在 CPU 上不实用**：注意力是 O(seq²)，
#: 8192 配 padding 一次要申请 22GB（实测直接 OOM）。1024 是这个规模下能用的值，
#: 也决定了**必须窗口化**——切片平均 6979 字，一个窗口装不下整片（见 pipeline/embed.py）。
MAX_TOKENS = 1024

_lock = threading.Lock()
_session = None
_tokenizer = None


def _root() -> Path:
    """资源根：开发时是仓库根，打包后是解包目录（与 `heavy_deps._root` 同一条理由）。"""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    return Path(__file__).resolve().parents[2]


ROOT = _root()


def cache_dir() -> Path:
    """缓存落在应用数据目录下（跟着用户的库走，能被一起备份/搬走）。"""
    return get_settings().data_dir / "cache" / "embed"


def files_dir() -> Path:
    """下载下来的**原始**文件（wheel 与模型）。校验就在这一层做。"""
    return cache_dir() / "files"


def site_dir() -> Path:
    """wheel 解出来的 Python 包目录 —— `sys.path` 加的是它。"""
    return cache_dir() / "site"


def manifest_path() -> Path:
    return ROOT / "build" / MANIFEST_FILE


def manifest() -> dict:
    if not manifest_path().is_file():
        return {}
    try:
        data = json.loads(manifest_path().read_text(encoding="utf-8"))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _tags() -> tuple[str, str]:
    """本机的 `(平台, Python 次版本)`，用来挑清单里该下哪几个文件。"""
    machine = platform.machine().lower()
    system = platform.system().lower()
    if system == "windows":
        plat = "windows-amd64" if machine in ("amd64", "x86_64") else f"windows-{machine}"
    else:
        plat = "linux-x86_64" if machine in ("x86_64", "amd64") else f"linux-{machine}"
    version = "%d.%d" % (sys.version_info.major, sys.version_info.minor)
    return plat, version


def wanted() -> dict:
    """清单里**本机该要**的那些文件（`any` 表示与平台/版本无关）。"""
    plat, version = _tags()
    out = {}
    for name, meta in (manifest().get("files") or {}).items():
        if not isinstance(meta, dict):
            continue
        want_plat = str(meta.get("platform") or "any")
        want_py = str(meta.get("python") or "any")
        if want_plat not in ("any", plat) or want_py not in ("any", version):
            continue
        out[str(name)] = meta
    return out


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _missing(*, strict: bool = False) -> list[str]:
    """还差哪些文件（没下、大小不对、或哈希对不上）。**先看在不在再算哈希**。

    `strict=True` 才逐个算 sha256（**只在下载完成时**用一次）；默认走
    **存在 + 大小**这条快路。

    为什么必须分两档：`is_ready()` 是**每次检索**都会调的，而 601MB 每算一次
    sha256 要 **350ms**（实测踩到：一次查询 367ms 里有 350ms 花在这儿，
    真正的向量计算只有 14ms）。大小对不上的（下载被截断）照样抓得出来；
    同长度的损坏抓不出 —— 那个由 `ensure()` 末尾的严格校验兜住。
    """
    need = wanted()
    if not need:
        return []
    out = []
    for name, meta in sorted(need.items()):
        path = files_dir() / name
        if not path.is_file():
            out.append(name)
            continue
        want_bytes = int((meta or {}).get("bytes") or 0)
        if want_bytes and path.stat().st_size != want_bytes:
            out.append(name)
            continue
        if strict and sha256_of(path) != str((meta or {}).get("sha256") or ""):
            out.append(name)
    return out


def _packages_importable() -> bool:
    """解出来的包里，`onnxruntime` 与 `tokenizers` 在不在。

    只查目录在不在 —— 那是**解包**的产物，可重建，不必逐个算哈希
    （真东西的哈希在 `files/` 那一层已经校验过了）。
    """
    site = site_dir()
    return (site / "onnxruntime").is_dir() and (site / "tokenizers").is_dir()


def is_ready(*, strict: bool = False) -> bool:
    """模型是不是就绪。**默认走快路**（存在 + 大小），因为它在每次检索里都被调用。

    要"确定没被改坏"的地方（`ensure()` 的收尾）传 `strict=True` ——
    **严格校验放在少见的那条路上，热路径不吃那 350ms**。
    """
    return bool(wanted()) and not _missing(strict=strict) and _packages_importable()


def status() -> dict:
    """给设置面板与工具看的现状（**不触发任何下载**）。"""
    need = wanted()
    total = sum(int((m or {}).get("bytes") or 0) for m in need.values())
    have = [name for name in need if (files_dir() / name).is_file()]
    ready = is_ready()
    return {
        "ready": ready,
        "files": len(need),
        "downloaded": len(have),
        "bytes": total,
        "mb": round(total / 1024 / 1024, 1),
        "model": str(manifest().get("model") or ""),
        "dim": int(manifest().get("dim") or 0),
        "cache": str(cache_dir()),
        "note": (
            "本地小模型已就绪"
            if ready
            else ("还没取（约 %.1f MB，首次使用时下载）" % (total / 1024 / 1024) if need
                  else "没有清单文件，取不了")
        ),
    }


def activate() -> None:
    """把解包目录挂进 `sys.path`（**幂等**）。

    公开的：`semantic` 也要用它 —— numpy 就在这个目录里，而算余弦要用 numpy
    （千级窗口 × 1024 维，纯 Python 要一秒多，numpy 是几毫秒）。
    """
    site = str(site_dir())
    if site not in sys.path:
        sys.path.insert(0, site)


def _copy_from_vendor(log: Callable[[str], None]) -> bool:
    """开发机 / 离线开发：仓库 `vendor/embed/` 里有一份，直接拷，**不联网**。

    与 Pyodide 同一条路（`heavy_deps._copy_from_vendor`）。仓库里没有这个目录
    也完全正常 —— 那就走下载。
    """
    vendor = ROOT / "vendor" / "embed"
    if not (vendor / "files").is_dir():
        return False
    target = files_dir()
    target.mkdir(parents=True, exist_ok=True)
    copied = 0
    for src in sorted((vendor / "files").iterdir()):
        if src.is_file():
            shutil.copy2(src, target / src.name)
            copied += 1
    log(f"本地模型：从仓库 vendor/embed 拷入 {copied} 个文件（未联网）")
    return True


def _download_one(name: str, url: str, want: str, log: Callable[[str], None],
                  index: int, count: int) -> bool:
    target = files_dir() / name
    if target.is_file() and sha256_of(target) == want:
        return True
    if not url:
        log(f"本地模型：{name} 在清单里没有下载地址")
        return False
    log(f"本地模型：下载 {name}（{index}/{count}）")
    partial = target.with_suffix(target.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=300) as response:  # noqa: S310
            with partial.open("wb") as handle:
                shutil.copyfileobj(response, handle)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        partial.unlink(missing_ok=True)
        log(f"本地模型：下载失败（{name}）—— {exc}")
        return False
    got = sha256_of(partial)
    if got != want:
        partial.unlink(missing_ok=True)
        log(f"本地模型：{name} 校验不过（期望 {want[:12]}… 实际 {got[:12]}…）")
        return False
    partial.replace(target)
    return True


def _extract_wheels(log: Callable[[str], None]) -> bool:
    """把校验过的 wheel 解到 `site/`。

    为什么是解包而不是 `pip install`：**这是给最终用户的机器做的** ——
    它可能没有 pip、也不该让应用去改用户的 Python 环境。wheel 本来就是 zip，
    解开、挂上 `sys.path`，就够了（实测 tokenizers 这样就能 import）。
    """
    site = site_dir()
    site.mkdir(parents=True, exist_ok=True)
    for name in sorted(wanted()):
        if not name.endswith(".whl"):
            continue
        try:
            with zipfile.ZipFile(files_dir() / name) as archive:
                archive.extractall(site)
        except (zipfile.BadZipFile, OSError) as exc:
            log(f"本地模型：解包失败（{name}）—— {exc}")
            return False
    return True


def ensure(*, allow_download: bool = True,
           log: Callable[[str], None] | None = None) -> tuple[bool, str]:
    """把本地模型准备好。**幂等**，多线程同时调用只跑一次。

    返回 `(是否可用, 一句人话的说明)` —— 失败时这句话要能直接讲给用户听。
    """
    say = log or (lambda _message: None)
    with _lock:
        if is_ready(strict=True):
            return True, "本地已有模型"
        if not wanted():
            return False, "没有清单文件（build/embed-manifest.json），取不了本地模型"

        if not _copy_from_vendor(say) and not allow_download:
            return False, "离线且本机没有模型副本 —— 语义检索这一项暂时用不了"

        need = wanted()
        files_dir().mkdir(parents=True, exist_ok=True)
        for index, name in enumerate(sorted(need), start=1):
            meta = need[name] or {}
            if not _download_one(name, str(meta.get("url") or ""),
                                 str(meta.get("sha256") or ""), say, index, len(need)):
                return False, "取不到本地模型（没网或下载失败）—— 语义检索这一项暂时用不了"

        if not _extract_wheels(say):
            return False, "本地模型下载完了，但解包失败 —— 语义检索这一项暂时用不了"

        if not is_ready(strict=True):
            return False, "取回来的东西对不上（校验/解包有问题）—— 语义检索这一项暂时用不了"

        total = sum((files_dir() / n).stat().st_size for n in need if (files_dir() / n).is_file())
        say(f"本地模型：就绪（{total / 1024 / 1024:.1f} MB）→ {cache_dir()}")
        return True, "已下载到本机缓存，之后离线可用"


_prefetching = False


def start_prefetch(log: Callable[[str], None] | None = None) -> bool:
    """在**后台线程**里把模型准备好（已就绪或已在准备就返回 False）。

    为什么不就地同步下载：150MB 级的东西会把那次调用挂住几分钟 ——
    界面上看起来就是卡死。所以"准备好"放后台，调用方如实告诉用户"正在取"
    （与 `heavy_deps.start_prefetch` 同一条理由）。
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
                log(f"本地模型：{'就绪' if ok else '没有就绪'} —— {detail}")
        finally:
            with _lock:
                _prefetching = False

    threading.Thread(target=_work, name="qf-embed-prefetch", daemon=True).start()
    return True


# ------------------------------------------------------------------ 推理


def _load():
    """载入 onnx 会话与分词器（**只载一次**）。

    输入名从 `session.get_inputs()` 现读、不写死：各家导出的 ONNX 有的叫
    `input_ids` / `attention_mask` / `token_type_ids`，有的少一两样 ——
    写死一个就会在"换了个模型"时静默报错。
    """
    global _session, _tokenizer
    if _session is not None:
        return _session, _tokenizer
    with _lock:
        if _session is not None:
            return _session, _tokenizer
        if not is_ready(strict=True):
            raise RuntimeError("本地模型还没准备好 —— 先 ensure() 或 start_prefetch()")
        activate()
        import onnxruntime  # noqa: PLC0415
        from tokenizers import Tokenizer  # noqa: PLC0415

        model_file = str(manifest().get("onnx") or "model.onnx")
        _session = onnxruntime.InferenceSession(
            str(files_dir() / model_file), providers=["CPUExecutionProvider"]
        )
        tokenizer = Tokenizer.from_file(str(files_dir() / str(manifest().get("tokenizer") or "tokenizer.json")))
        tokenizer.enable_truncation(max_length=MAX_TOKENS)
        tokenizer.enable_padding()
        _tokenizer = tokenizer
        return _session, _tokenizer


def embed(texts: list[str], *, kind: str = "passage") -> list[list[float]]:
    """把一批文本变成**已归一化**的向量。

    归一化在这里做（而不是让调用方做）：余弦相似度要求两个向量同尺度，
    而"忘了归一化"是个不会报错、只会让分数变得没意义的错
    （与 `semantic.cosine` 那条"不假设已归一化"是一对：这边保证给出归一化的，
    那边不依赖归一化 —— 任一侧单独改动都不会坏）。

    `kind` 是 `"query"` 或 `"passage"`，决定加哪个前缀（见模块 docstring）。
    """
    if not texts:
        return []
    if kind not in PREFIX:
        raise ValueError("kind 只能是 query 或 passage：" + str(kind))
    session, tokenizer = _load()
    import numpy as np  # noqa: PLC0415 —— 必须等 activate() 之后

    encoded = tokenizer.encode_batch([PREFIX[kind] + str(text) for text in texts])
    ids = np.array([item.ids for item in encoded], dtype=np.int64)
    mask = np.array([item.attention_mask for item in encoded], dtype=np.int64)

    feed = {}
    for entry in session.get_inputs():
        if entry.name in ("input_ids", "input_ids:0"):
            feed[entry.name] = ids
        elif entry.name in ("attention_mask", "attention_mask:0"):
            feed[entry.name] = mask
        elif "token_type" in entry.name:
            feed[entry.name] = np.zeros_like(ids)

    outputs = session.run(None, feed)
    last = np.asarray(outputs[0], dtype="float32")  # (batch, seq, hidden)
    weights = mask[..., None].astype("float32")
    pooled = (last * weights).sum(axis=1) / np.maximum(weights.sum(axis=1), 1e-9)
    norms = np.linalg.norm(pooled, axis=1, keepdims=True)
    return (pooled / np.maximum(norms, 1e-9)).tolist()


def pack(vector: list[float]) -> bytes:
    """与 `semantic.pack` 同一个格式（float32 小端）—— 那边负责读，这里只写入。

    刻意**不** import `semantic`：那会把 api 侧的模块拖进 pipeline 的导入链，
    而这个函数只有三行。格式一致性由 `tests/test_semantic.py` 的往返用例盯着。
    """
    return struct.pack("<%df" % len(vector), *[float(x) for x in vector])


def dim() -> int:
    return int(manifest().get("dim") or 0)


def describe() -> str:
    info = status()
    return "本地小模型 · %s · %s 维 · 约 %.1f MB" % (
        info["model"] or "未知", info["dim"] or "?", info["mb"]
    )


def clear() -> None:
    """删掉缓存（**给测试与排障用**；界面不暴露）。"""
    global _session, _tokenizer
    with _lock:
        _session = None
        _tokenizer = None
        shutil.rmtree(cache_dir(), ignore_errors=True)
