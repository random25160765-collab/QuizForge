#!/usr/bin/env python3
"""把第三方前端库同步到 vendor/。

两份东西：

* **KaTeX**（必需，公式渲染）：建构建**不依赖网络** —— 从本机已有的副本同步
  （HTTPS 出网在本机时好时坏，构建不该被这件事卡住）。
* **Pyodide**（可选，对话里跑 Python 的执行环境）：来源顺序是
  `QUIZFORGE_PYODIDE_SRC` → 已有副本 → 钉死版本的 CDN。它是 13MB 的运行时，
  取一次就够；取不到也只影响「跑 Python」这一项能力，不影响构建。

来源自动探测顺序（KaTeX）：
  1. 环境变量 QUIZFORGE_KATEX_SRC 指向的目录
  2. 本项目 vendor/katex/（已同步过则直接跳过）
  3. 本机常见位置（用户自己的博客仓库 node_modules 等）

同步动作：
  - katex.min.js            -> vendor/katex/katex.min.js
  - katex.min.css           -> 裁剪后写为 vendor/katex/katex.css
                               （去掉 woff / ttf 回退，只保留 woff2）
  - fonts/*.woff2           -> vendor/katex/fonts/*.woff2
  - pyodide/*（5 个文件）    -> vendor/pyodide/
  - SOURCE.md               -> 记录来源、版本、时间（便于日后追溯）

用法：
    python3 tools/vendor.py            # 同步（已存在则跳过，除非 --force）
    python3 tools/vendor.py --force    # 强制重新同步
    python3 tools/vendor.py --check    # 只检查 vendor 是否就绪
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

#: 仓库根（清单脚本 `build/package.py` 在那儿 —— 见 sync_pyodide 末尾的刷新）
REPO_ROOT = Path(__file__).resolve().parents[1]

ROOT = Path(__file__).resolve().parent.parent
VENDOR_KATEX = ROOT / "vendor" / "katex"

# ---------------------------------------------------------------- Pyodide
#
# 它是「对话里跑 Python」（`run_python` 工具）的执行环境。走 CDN 的话，
# 沙箱要现下 13MB 运行时 —— 实测在演示面板里挂了 60 秒仍不见成功，
# 用户看到的就是「正在加载运行时」停在那儿。本机一份之后，面板一开就能跑，
# 而且**不再依赖网络**（沙箱为了跑一段脚本去联网，本来就是件别扭的事）。
#
# 版本钉死：CDN 上的东西会变，而这份副本要能重放。
VENDOR_PYODIDE = ROOT / "vendor" / "pyodide"
PYODIDE_VERSION = "0.26.4"
PYODIDE_CDN = f"https://cdn.jsdelivr.net/pyodide/v{PYODIDE_VERSION}/full/"
# 只有这 5 个文件是「跑一段脚本」必需的（整份发行版还带一堆可选包，不要）
PYODIDE_FILES = (
    "pyodide.js",
    "pyodide.asm.js",
    "pyodide.asm.wasm",
    "python_stdlib.zip",
    "pyodide-lock.json",
)
# 预置的包（依赖会从 lock 里解出来，比如 scipy → numpy + openblas）。
#
# 分两拨（`_PRELOAD` 与这里全部的区别见 `api/app/tools.py` 的注释）：
#   * numpy / scipy —— **壳启动时就装好**（最常用，装了就一直现成）；
#   * pandas / matplotlib —— 只要文件在 indexURL 旁边就行，**脚本 import 了才装**
#     （`loadPackagesFromImports` 在壳里做），没用到的人一个字节都不下。
# 体积是真的：numpy 12MB、openblas 6MB、scipy 45MB、pandas 13MB、
# matplotlib 10MB 加上它那一串依赖（pillow / kiwisolver / fonttools 等）。
#
# 为什么要预置：Pyodide 按 `indexURL` 找包（运行时目录里必须有那些 .whl），
# 不在那儿就是"没有这个模块"。实测就是这么缺的 —— 模型在演示里
# `import numpy`，面板上却是 `No module named 'numpy'`。
#
# 体积是真的：numpy 12MB、openblas 6MB、scipy 45MB。想加包就往这里加名字。
PYODIDE_PACKAGES = (
    "numpy",
    "scipy",
    "pandas",
    "matplotlib",
    # sympy / networkx：EE 那类课要的是**推导**（小信号等效 → KCL/KVL → 把 A_v(s) 化简
    # 成标准二阶形式），数值算给不出一支带参数的表达式。两个都是纯 Python。
    "sympy",
    "networkx",
    # micropip：**壳自己要用的**（见 EXTRA_WHEELS —— 它靠 micropip 从本机 URL 装那些
    # lock 之外的包）。它虽然写在 pyodide 的 lock 里，但不在上面这串的依赖闭包里 ——
    # 不显式列出来的话 `make vendor` 不会取它，壳里 `loadPackage("micropip")` 就是 404
    #（实测栽过：报"You can install it by calling: await micropip.install('micropip')"）。
    "micropip",
)

#: **lock 里没有、但我们自己要的包**（Pyodide 的 `loadPackage` 只认 lock 里那 310 个，
#: 而 schemdraw 不在其中）。
#:
#: 好在它 152KB、纯 Python、核心零依赖（matplotlib 只是画图时的 extra）——
#: 把 wheel 原样放进 indexURL 旁边，壳里用 `micropip.install("<本机 URL>")` 装就行，
#: 仍然**不联网**（和别的包一个规矩）。版本钉死，取回来的东西可复现。
EXTRA_WHEELS: tuple[tuple[str, str], ...] = (
    # (包名, 版本)
    ("schemdraw", "0.23"),
)

# ---------------------------------------------------------------- 演示套件
#
# 对话里的演示（`render_demo`）跑在沙箱 iframe 里。这些库由服务端注入到那一页，
# 模型因此不必自己写 `<script src>`（也就不必猜版本、不必担心 CDN 挂掉）。
# 版本同样钉死。
VENDOR_DEMO_KIT = ROOT / "vendor" / "demo-kit"
DEMO_KIT_SOURCES = {
    "react.js": "https://unpkg.com/react@18.3.1/umd/react.production.min.js",
    "react-dom.js": "https://unpkg.com/react-dom@18.3.1/umd/react-dom.production.min.js",
    "htm.js": "https://unpkg.com/htm@3.1.1/dist/htm.js",
    "d3.js": "https://unpkg.com/d3@7.9.0/dist/d3.min.js",
    "babel.js": "https://unpkg.com/@babel/standalone@7.26.4/babel.min.js",
    "tailwind.js": "https://cdn.tailwindcss.com/3.4.17",
}

# ---------------------------------------------------------------- 代码排版
#
# 见 `sync_frontend`：一份真等宽字体 + 一个真正的高亮器。
VENDOR_MONO = ROOT / "vendor" / "mono"
VENDOR_HLJS = ROOT / "vendor" / "hljs"
HLJS_FILES_MONO = {
    "jetbrains-mono-latin-400-normal.woff2": (
        "https://cdn.jsdelivr.net/npm/@fontsource/jetbrains-mono@5.1.1/files/"
        "jetbrains-mono-latin-400-normal.woff2"
    ),
    "jetbrains-mono-latin-700-normal.woff2": (
        "https://cdn.jsdelivr.net/npm/@fontsource/jetbrains-mono@5.1.1/files/"
        "jetbrains-mono-latin-700-normal.woff2"
    ),
}
HLJS_FILES_HLJS = {
    "highlight.min.js": (
        "https://cdn.jsdelivr.net/npm/@highlightjs/cdn-assets@11.11.1/highlight.min.js"
    ),
}

# 本机已知的 KaTeX 分布位置（按优先级）。这些路径只读，绝不修改。
CANDIDATE_SOURCES = [
    "~/Source/random25160765-collab.github.io/node_modules/katex/dist",
    "~/Source/random25160765-collab.github.io/node_modules/.pnpm/katex@0.17.0/node_modules/katex/dist",
    "~/Source/random25160765-collab.github.io/node_modules/.pnpm/katex@0.16.47/node_modules/katex/dist",
    "~/Desktop/Codebase/random25160765-collab.github.io/node_modules/katex/dist",
]

# 只保留 woff2：woff/ttf 是给老旧浏览器用的回退，内联进 HTML 会显著增大体积。
_FONT_SRC_RE = re.compile(r"url\(fonts/([\w.-]+?)\.(woff2|woff|ttf)\)")


def _candidates() -> list[Path]:
    env = os.environ.get("QUIZFORGE_KATEX_SRC")
    paths: list[Path] = []
    if env:
        paths.append(Path(env).expanduser())
    paths.extend(Path(p).expanduser() for p in CANDIDATE_SOURCES)
    return paths


def find_katex_source() -> Path | None:
    """返回第一个同时含 katex.min.js 与 katex.min.css 的目录。"""
    for path in _candidates():
        if (path / "katex.min.js").is_file() and (path / "katex.min.css").is_file():
            return path
    return None


def _strip_font_fallbacks(css: str) -> str:
    """把 @font-face 里的 woff/ttf 回退去掉，只留 woff2。

    KaTeX 的 src 形如：
        url(fonts/X.woff2) format("woff2"),url(fonts/X.woff) format("woff"),url(fonts/X.ttf) format("truetype")
    裁剪后成为：
        url(fonts/X.woff2) format("woff2")
    """

    def repl(match: re.Match[str]) -> str:
        name, ext = match.group(1), match.group(2)
        return match.group(0) if ext == "woff2" else f"url(data:,)"

    trimmed = _FONT_SRC_RE.sub(repl, css)
    # 清掉被替换成空 data URL 的条目及其 format() 声明
    trimmed = re.sub(r",?\s*url\(data:,\)\s*format\(\s*['\"]?(?:woff|truetype)['\"]?\s*\)", "", trimmed)
    trimmed = re.sub(r"url\(data:,\)\s*format\(\s*['\"]?(?:woff|truetype)['\"]?\s*\)\s*,?", "", trimmed)
    return trimmed


def _katex_version(src: Path) -> str:
    """从最近的 package.json 里读版本号，读不到就返回 unknown。"""
    for parent in [src, *src.parents[:4]]:
        pkg = parent / "package.json"
        if pkg.is_file():
            try:
                text = pkg.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            m = re.search(r'"version"\s*:\s*"([^"]+)"', text)
            if m and "katex" in text.lower():
                return m.group(1)
    # .pnpm/katex@0.16.47/... 这种路径里带着版本号
    m = re.search(r"katex@([\d.]+)", str(src))
    return m.group(1) if m else "unknown"


def _download(url: str, dest: Path) -> None:
    """下载一个文件。用标准库，不引依赖（这是唯一需要联网的一步）。"""
    import urllib.request

    request = urllib.request.Request(url, headers={"User-Agent": "quizforge-vendor"})
    with urllib.request.urlopen(request, timeout=180) as response:
        with dest.open("wb") as out:
            shutil.copyfileobj(response, out)


def _pypi_wheel_url(name: str, version: str) -> str:
    """从 PyPI 的 JSON API 找这个版本、这个**纯 Python** wheel 的地址。

    为什么不拼 `files.pythonhosted.org` 那种带哈希的路径：它随重新上传会变，
    钉不住；而 API 给的是当前有效的那个。
    """
    import urllib.request

    url = f"https://pypi.org/pypi/{name}/{version}/json"
    request = urllib.request.Request(url, headers={"User-Agent": "quizforge-vendor"})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = json.loads(response.read().decode("utf-8"))
    for item in data.get("urls") or []:
        if str(item.get("filename") or "").endswith("-py3-none-any.whl"):
            return str(item["url"])
    raise RuntimeError(f"PyPI 上没有 {name} {version} 的 py3-none-any wheel")


def pyodide_package_files(names=PYODIDE_PACKAGES) -> list[str]:
    """按 pyodide-lock.json 解析出这些包（含依赖）的**文件名**。

    为什么不手拼文件名：它带 abi 与版本
    （`numpy-1.26.4-cp312-cp312-pyodide_2024_0_wasm32.whl`），猜错了就是 404 ——
    而 404 的表现是运行时"没有这个模块"，离真正的原因很远（实测就吃了这一下）。
    """
    lock_path = VENDOR_PYODIDE / "pyodide-lock.json"
    if not lock_path.is_file():
        return []
    try:
        packages = json.loads(lock_path.read_text(encoding="utf-8")).get("packages") or {}
    except (OSError, ValueError):
        return []
    out: list[str] = []
    seen: set[str] = set()
    queue = list(names)
    while queue:
        name = queue.pop(0)
        if name in seen or name not in packages:
            continue
        seen.add(name)
        out.append(str(packages[name].get("file_name") or ""))
        queue.extend(packages[name].get("depends") or [])
    return [name for name in out if name]


def sync_pyodide(force: bool = False) -> int:
    """把 Pyodide 运行时（含 `PYODIDE_PACKAGES` 里预置的包）同步到 vendor/pyodide/。

    来源顺序：`QUIZFORGE_PYODIDE_SRC` → 已有的 vendor/pyodide/ → 钉死版本的 CDN。
    **同步失败不当成致命错误**：Python 跑不了是少一项能力，不该让整个构建停下 ——
    但要大声说出来，否则会变成"演示面板一直转圈"那种没人知道为什么的故障。

    包和运行时放在**同一个目录**：Pyodide 按 `indexURL` 找 `.whl`，放别处它找不到
    （实测的表现是运行时 `No module named 'numpy'`，而原因离得很远）。
    """
    src_env = os.environ.get("QUIZFORGE_PYODIDE_SRC")
    source_dir = Path(src_env).expanduser() if src_env else None
    VENDOR_PYODIDE.mkdir(parents=True, exist_ok=True)

    core_missing = [name for name in PYODIDE_FILES if not (VENDOR_PYODIDE / name).is_file()]
    origin_text = _rel(VENDOR_PYODIDE)
    if core_missing or force:
        if source_dir and source_dir.is_dir():
            for name in PYODIDE_FILES:
                origin = source_dir / name
                if not origin.is_file():
                    print(f"[ERROR] 来源目录缺 {name}：{source_dir}", file=sys.stderr)
                    return 2
                shutil.copyfile(origin, VENDOR_PYODIDE / name)
            origin_text = _origin(source_dir)
        else:
            print(f"[INFO] 从 CDN 取 Pyodide {PYODIDE_VERSION} 运行时（约 13MB，只此一次）")
            for name in PYODIDE_FILES:
                if (VENDOR_PYODIDE / name).is_file() and not force:
                    continue
                try:
                    _download(PYODIDE_CDN + name, VENDOR_PYODIDE / name)
                except Exception as exc:  # noqa: BLE001
                    print(f"[WARN] 取 {name} 失败：{exc}", file=sys.stderr)
                    print(
                        "       Python 沙箱将退回 CDN（能联网时可用）。"
                        "要离线可用，请用 QUIZFORGE_PYODIDE_SRC 指向一份本地副本。",
                        file=sys.stderr,
                    )
                    return 1
            origin_text = PYODIDE_CDN
    else:
        print(f"[INFO] Pyodide 运行时已就绪：{_rel(VENDOR_PYODIDE)}")

    # 预置包：核心文件到位之后才能读 lock，所以放在这一步之后
    wanted = pyodide_package_files()
    if not wanted:
        print("[WARN] 读不出 pyodide-lock.json，跳过预置包", file=sys.stderr)
    packages_missing = [name for name in wanted if force or not (VENDOR_PYODIDE / name).is_file()]
    for name in packages_missing:
        print(f"[INFO] 取预置包 {name} …")
        origin = (source_dir / name) if (source_dir and source_dir.is_dir()) else None
        try:
            if origin and origin.is_file():
                shutil.copyfile(origin, VENDOR_PYODIDE / name)
            else:
                _download(PYODIDE_CDN + name, VENDOR_PYODIDE / name)
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] 取 {name} 失败：{exc}", file=sys.stderr)
            print(
                "       那些包在沙箱里表现为「没有这个模块」（run_python 会把报错打在面板上）。",
                file=sys.stderr,
            )
            return 1

    # 额外 wheel（lock 里没有的，比如 schemdraw）：从 PyPI 取**纯 Python** 那一份，
    # 钉版本。与预置包一样放在 indexURL 旁边 —— 壳里用 micropip 从**本机** URL 装，
    # 仍然不联网。
    for name, version in EXTRA_WHEELS:
        filename = f"{name}-{version}-py3-none-any.whl"
        target = VENDOR_PYODIDE / filename
        if target.is_file() and not force:
            continue
        print(f"[INFO] 取额外包 {filename} …")
        try:
            _download(_pypi_wheel_url(name, version), target)
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] 取 {name} 失败：{exc}", file=sys.stderr)
            print("       沙箱里表现为「没有这个模块」。", file=sys.stderr)
            return 1

    have = [name for name in wanted if (VENDOR_PYODIDE / name).is_file()]
    total = sum((VENDOR_PYODIDE / name).stat().st_size for name in have)
    (VENDOR_PYODIDE / "SOURCE.md").write_text(
        "# vendored Pyodide\n\n"
        f"- version: {PYODIDE_VERSION}\n"
        f"- source: {origin_text}\n"
        f"- 运行时: {len(PYODIDE_FILES)} 个文件\n"
        f"- 预置包: {len(have)} 个（{total // 1024 // 1024}MB）"
        " —— " + ", ".join(PYODIDE_PACKAGES) + "\n"
        f"- synced_at: {_dt.datetime.now().isoformat(timespec='seconds')}\n"
        "\n由 `tools/vendor.py` 生成，请勿手工修改。\n"
        "用途：对话里的 run_python 工具在沙箱 iframe 里真跑 Python；\n"
        "包必须与 pyodide.js 同目录 —— Pyodide 按 indexURL 找 .whl。\n",
        encoding="utf-8",
    )
    print(f"[INFO] Pyodide 已同步：运行时 {len(PYODIDE_FILES)} + 预置包 {len(have)} 个 -> {_rel(VENDOR_PYODIDE)}")

    # **顺手把清单刷新**：`/assets/pyodide/{name}` 只发**清单里列着**的文件
    #（见 `heavy_deps.file_path` —— 它拿清单挡路径穿越，顺带也挡住了没登记的包）。
    # 清单旧了的表现是**新取的包一律 404**，而 404 的现场很难认：
    # `loadPackage` 静默跳过（不报错！），直到 `import` 时才说"没有这个模块"。
    # 这一次就栽在这儿：sympy / networkx / schemdraw / micropip 全在缓存里，
    # 一个都取不到。让"取包"和"刷清单"永远是一件事，别靠人记得。
    try:
        subprocess.run(
            [sys.executable, str(REPO_ROOT / "build" / "package.py"), "manifest"],
            check=True,
            capture_output=True,
        )
        print("[INFO] 运行时清单已刷新：build/pyodide-manifest.json")
    except Exception as exc:  # noqa: BLE001
        print(
            f"[WARN] 清单没刷新（{exc}）—— 新取的包会取不到，手跑一次 build/package.py manifest",
            file=sys.stderr,
        )
    return 0


def sync_demo_kit(force: bool = False) -> int:
    """把**演示沙箱用的前端套件**同步到 vendor/demo-kit/。

    演示页面是模型现写的一页 HTML，跑在沙箱 iframe 里。原先它是一张白纸：
    引哪个库、什么版本、怎么摆布局，全要模型每次自己决定 —— 结果就是"能跑但难看"。
    这一套装进沙箱之后，模型只写正文（那些库与样式由服务端统一注入，见
    `tools._demo_page`）。

    与 Pyodide 同样对待：可选、失败只警告（那会让演示退回"自己引 CDN"的老样子）。
    """
    ready = all((VENDOR_DEMO_KIT / name).is_file() for name in DEMO_KIT_SOURCES)
    if ready and not force:
        print(f"[INFO] 演示套件已就绪，跳过：{_rel(VENDOR_DEMO_KIT)}")
        return 0

    src_env = os.environ.get("QUIZFORGE_DEMO_KIT_SRC")
    source_dir = Path(src_env).expanduser() if src_env else None
    VENDOR_DEMO_KIT.mkdir(parents=True, exist_ok=True)

    if source_dir and source_dir.is_dir():
        for name in DEMO_KIT_SOURCES:
            origin = source_dir / name
            if not origin.is_file():
                print(f"[ERROR] 来源目录缺 {name}：{source_dir}", file=sys.stderr)
                return 2
            shutil.copyfile(origin, VENDOR_DEMO_KIT / name)
        origin_text = _origin(source_dir)
    else:
        for name, url in DEMO_KIT_SOURCES.items():
            print(f"[INFO] 取 {name} …")
            try:
                _download(url, VENDOR_DEMO_KIT / name)
            except Exception as exc:  # noqa: BLE001
                print(f"[WARN] 取 {name} 失败：{exc}", file=sys.stderr)
                print(
                    "       演示会退回「自己引 CDN」的老样子（能联网时仍可用）。"
                    "要离线可用，请用 QUIZFORGE_DEMO_KIT_SRC 指向一份本地副本。",
                    file=sys.stderr,
                )
                return 1
        origin_text = "unpkg / cdn.tailwindcss.com（版本见下表）"

    total = sum((VENDOR_DEMO_KIT / name).stat().st_size for name in DEMO_KIT_SOURCES)
    lines = "\n".join(f"- {name}: {url}" for name, url in DEMO_KIT_SOURCES.items())
    (VENDOR_DEMO_KIT / "SOURCE.md").write_text(
        "# vendored 演示套件\n\n"
        f"- source: {origin_text}\n"
        f"- files: {len(DEMO_KIT_SOURCES)}（{total // 1024}KB）\n"
        f"- synced_at: {_dt.datetime.now().isoformat(timespec='seconds')}\n\n"
        f"{lines}\n\n"
        "由 `tools/vendor.py` 生成，请勿手工修改。\n"
        "用途：对话里 render_demo 生成的演示页在沙箱 iframe 里跑，这些库由服务端注入。\n",
        encoding="utf-8",
    )
    print(f"[INFO] 已同步演示套件：{len(DEMO_KIT_SOURCES)} 个文件 -> {_rel(VENDOR_DEMO_KIT)}")
    return 0


def sync_frontend(force: bool = False) -> int:
    """同步**代码排版**要用的两样：真字体与真正的语法高亮。

    * **JetBrains Mono**（24KB×2）：`--font-mono` 首选就是它，但本机
      （Linux）没有 SF Mono / Menlo / Consolas，实际落到 `DejaVu Sans Mono` ——
      等宽字里它是难看的那一类，而代码块满屏都是。带一份真字体就够了。
    * **highlight.js**（128KB，含常用语言）：原先的高亮是**手写的**
      （`theme/runtime/highlight.js`，当年本机无外网），token 切得粗。
      hljs 到位就优先用它，手写那份留作兜底。
    """
    jobs = (
        (VENDOR_MONO, HLJS_FILES_MONO),
        (VENDOR_HLJS, HLJS_FILES_HLJS),
    )
    missing_total = 0
    for target_dir, sources in jobs:
        target_dir.mkdir(parents=True, exist_ok=True)
        missing = [
            name for name in sources if force or not (target_dir / name).is_file()
        ]
        if not missing:
            print(f"[INFO] 已就绪，跳过：{_rel(target_dir)}")
            continue
        for name in missing:
            print(f"[INFO] 取 {name} …")
            try:
                _download(sources[name], target_dir / name)
            except Exception as exc:  # noqa: BLE001
                print(f"[WARN] 取 {name} 失败：{exc}", file=sys.stderr)
                print(
                    "       代码块会退回手写高亮与系统等宽字体（能用，但没那么好看）。",
                    file=sys.stderr,
                )
                missing_total += 1
    return 1 if missing_total else 0


def sync(force: bool = False) -> int:
    """同步 vendor 下的第三方资源。

    KaTeX 必需（缺了页面公式会退化）；其余（Pyodide / 演示套件 / 字体与高亮）
    都是**可选**的 —— 缺了各少一项能力或好看度，不该让构建停下。
    """
    code = sync_katex(force=force)
    sync_pyodide(force=force)
    sync_demo_kit(force=force)
    sync_frontend(force=force)
    return code


def sync_katex(force: bool = False) -> int:
    if VENDOR_KATEX.is_dir() and (VENDOR_KATEX / "katex.min.js").is_file() and not force:
        print(f"[INFO] vendor 已就绪，跳过：{_rel(VENDOR_KATEX)}")
        print("       （如需重新同步请加 --force）")
        return 0

    src = find_katex_source()
    if src is None:
        print("[ERROR] 找不到本机 KaTeX 副本，无法同步 vendor。", file=sys.stderr)
        print("        本机无外网，请不要尝试从 CDN 下载。", file=sys.stderr)
        print("        可尝试以下任一处理：", file=sys.stderr)
        for p in _candidates():
            print(f"          - 确认该路径存在：{p}", file=sys.stderr)
        print("          - 或用环境变量显式指定：", file=sys.stderr)
        print("            QUIZFORGE_KATEX_SRC=/path/to/katex/dist python3 tools/vendor.py", file=sys.stderr)
        return 2

    print(f"[INFO] KaTeX 来源：{src}")

    fonts_dst = VENDOR_KATEX / "fonts"
    fonts_dst.mkdir(parents=True, exist_ok=True)

    # 1) katex.min.js
    shutil.copyfile(src / "katex.min.js", VENDOR_KATEX / "katex.min.js")

    # 2) CSS：裁剪掉非 woff2 回退
    raw_css = (src / "katex.min.css").read_text(encoding="utf-8")
    css = _strip_font_fallbacks(raw_css)
    (VENDOR_KATEX / "katex.css").write_text(css, encoding="utf-8")

    # 3) 只拷贝被 CSS 引用到的 woff2 字体
    wanted = set()
    for m in _FONT_SRC_RE.finditer(css):
        if m.group(2) == "woff2":
            wanted.add(f"{m.group(1)}.woff2")
    if not wanted:
        print("[ERROR] 裁剪后的 CSS 里没有解析到任何 woff2 字体，KaTeX 版本可能不兼容。", file=sys.stderr)
        return 3

    copied, missing = 0, []
    for name in sorted(wanted):
        s = src / "fonts" / name
        if not s.is_file():
            missing.append(name)
            continue
        shutil.copyfile(s, fonts_dst / name)
        copied += 1

    if missing:
        print(f"[ERROR] 缺少字体文件：{', '.join(missing)}", file=sys.stderr)
        return 3

    # 4) 来源记录
    version = _katex_version(src)
    (VENDOR_KATEX / "SOURCE.md").write_text(
        "# vendored KaTeX\n\n"
        f"- version: {version}\n"
        f"- source: {_origin(src)}\n"
        f"- synced_at: {_dt.datetime.now().isoformat(timespec='seconds')}\n"
        f"- fonts: {copied} woff2 (woff/ttf fallbacks stripped)\n"
        f"- katex.min.js sha256: {_sha256(VENDOR_KATEX / 'katex.min.js')}\n"
        "\n由 `tools/vendor.py` 生成，请勿手工修改。\n"
        "本机无外网，此目录只能从本机已有副本同步。\n",
        encoding="utf-8",
    )

    print(f"[INFO] 已同步 KaTeX {version}：1 js + 1 css + {copied} woff2 字体 -> {_rel(VENDOR_KATEX)}")
    return 0


def check() -> int:
    ok = True
    for name in ("katex.min.js", "katex.css"):
        p = VENDOR_KATEX / name
        if not p.is_file():
            print(f"[ERROR] 缺失 {_rel(p)}", file=sys.stderr)
            ok = False
    fonts = sorted((VENDOR_KATEX / "fonts").glob("*.woff2")) if (VENDOR_KATEX / "fonts").is_dir() else []
    if not fonts:
        print("[ERROR] vendor/katex/fonts 下没有任何 woff2 字体", file=sys.stderr)
        ok = False
    if ok:
        print(f"[INFO] vendor 就绪：{_rel(VENDOR_KATEX)}（{len(fonts)} 个 woff2 字体）")

    # Pyodide 只报告，不算失败：它是"对话里跑 Python"的能力，不是构建前提
    missing = [name for name in PYODIDE_FILES if not (VENDOR_PYODIDE / name).is_file()]
    if missing:
        print(f"[INFO] Pyodide 未就绪（缺 {len(missing)} 个文件）—— 跑 `make vendor` 取一份")
    else:
        print(f"[INFO] Pyodide 就绪：{_rel(VENDOR_PYODIDE)}")

    return 0 if ok else 1


def _sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _origin(path: Path) -> str:
    """写进 `SOURCE.md` 的**来源位置** —— 不写开发机的绝对路径。

    三级退化：仓库内给相对仓库根的路径；家目录下给 `~/...`；都不沾
    （比如挂在别处的盘）才原样输出。

    `SOURCE.md` 是进版本库的（仓库公开），而绝对路径里带着某台机器的用户名和
    目录结构：换台机器就不成立，对别人也没有信息量。这里要说清的是
    \"从哪儿同步来的\"，而不是\"这台机器上是谁的目录\"。
    """
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        pass
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="同步 vendor 资源（不联网）")
    parser.add_argument("--force", action="store_true", help="强制重新同步")
    parser.add_argument("--check", action="store_true", help="只检查 vendor 是否就绪")
    args = parser.parse_args()
    return check() if args.check else sync(force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())
