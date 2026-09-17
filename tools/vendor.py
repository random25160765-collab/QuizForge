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
import os
import re
import shutil
import sys
from pathlib import Path

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


def sync_pyodide(force: bool = False) -> int:
    """把 Pyodide 运行时同步到 vendor/pyodide/。

    来源顺序：`QUIZFORGE_PYODIDE_SRC` → 已有的 vendor/pyodide/ → 钉死版本的 CDN。
    **同步失败不当成致命错误**：Python 跑不了是少一项能力，不该让整个构建停下 ——
    但要大声说出来，否则会变成"演示面板一直转圈"那种没人知道为什么的故障。
    """
    ready = all((VENDOR_PYODIDE / name).is_file() for name in PYODIDE_FILES)
    if ready and not force:
        print(f"[INFO] Pyodide 已就绪，跳过：{_rel(VENDOR_PYODIDE)}")
        return 0

    src_env = os.environ.get("QUIZFORGE_PYODIDE_SRC")
    source_dir = Path(src_env).expanduser() if src_env else None
    VENDOR_PYODIDE.mkdir(parents=True, exist_ok=True)

    if source_dir and source_dir.is_dir():
        for name in PYODIDE_FILES:
            origin = source_dir / name
            if not origin.is_file():
                print(f"[ERROR] 来源目录缺 {name}：{source_dir}", file=sys.stderr)
                return 2
            shutil.copyfile(origin, VENDOR_PYODIDE / name)
        origin_text = str(source_dir)
    else:
        print(f"[INFO] 从 CDN 取 Pyodide {PYODIDE_VERSION}（约 13MB，只此一次）：{PYODIDE_CDN}")
        for name in PYODIDE_FILES:
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

    total = sum((VENDOR_PYODIDE / name).stat().st_size for name in PYODIDE_FILES)
    (VENDOR_PYODIDE / "SOURCE.md").write_text(
        "# vendored Pyodide\n\n"
        f"- version: {PYODIDE_VERSION}\n"
        f"- source: {origin_text}\n"
        f"- files: {len(PYODIDE_FILES)}（{total // 1024 // 1024}MB）\n"
        f"- synced_at: {_dt.datetime.now().isoformat(timespec='seconds')}\n"
        "\n由 `tools/vendor.py` 生成，请勿手工修改。\n"
        "用途：对话里的 run_python 工具在沙箱 iframe 里真跑 Python。\n",
        encoding="utf-8",
    )
    print(f"[INFO] 已同步 Pyodide {PYODIDE_VERSION}：{len(PYODIDE_FILES)} 个文件 -> {_rel(VENDOR_PYODIDE)}")
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
        origin_text = str(source_dir)
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


def sync(force: bool = False) -> int:
    """同步 vendor 下的第三方资源。

    KaTeX 必需（缺了页面公式会退化）；Pyodide 与演示套件都是**可选**的
    （缺了各少一项能力，不该让构建停下）。
    """
    code = sync_katex(force=force)
    sync_pyodide(force=force)
    sync_demo_kit(force=force)
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
        f"- source: {src}\n"
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


def main() -> int:
    parser = argparse.ArgumentParser(description="同步 vendor 资源（不联网）")
    parser.add_argument("--force", action="store_true", help="强制重新同步")
    parser.add_argument("--check", action="store_true", help="只检查 vendor 是否就绪")
    args = parser.parse_args()
    return check() if args.check else sync(force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())
