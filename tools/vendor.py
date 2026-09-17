#!/usr/bin/env python3
"""把第三方前端库同步到 vendor/。

本机实测无法访问外网（HTTPS 出网超时、无本地代理），因此这里**不做任何
网络请求**，只从本机已有的 KaTeX 副本同步 —— 构建因此不依赖网络。

来源自动探测顺序：
  1. 环境变量 QUIZFORGE_KATEX_SRC 指向的目录
  2. 本项目 vendor/katex/（已同步过则直接跳过）
  3. 本机常见位置（用户自己的博客仓库 node_modules 等）

同步动作：
  - katex.min.js            -> vendor/katex/katex.min.js
  - katex.min.css           -> 裁剪后写为 vendor/katex/katex.css
                               （去掉 woff / ttf 回退，只保留 woff2）
  - fonts/*.woff2           -> vendor/katex/fonts/*.woff2
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


def sync(force: bool = False) -> int:
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
        return 0
    return 1


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
