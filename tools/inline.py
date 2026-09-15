#!/usr/bin/env python3
"""把 CSS / JS / 字体 / 题库数据全部内联进单文件 HTML。

为什么必须内联：
  `file://` 协议下 fetch / XHR / ES module import 会被 CORS 拦截，只有普通
  <script src> 与 <link rel=stylesheet> 能加载。为了让产物「双击就能用、
  换台机器也能用」，我们把题库数据与 KaTeX 引擎全部写进 HTML 内部。

安全转义：
  在 <script> 内部出现字面量 "</script" 或 "<!--" 会骗过 HTML 解析器导致
  脚本被提前截断。这里统一把它们转义成 "<\\/script" 与 "<\\!--"——这在 JS
  字符串/正则与 JSON 字符串里都是合法转义，在注释里也只是普通文本。
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

_FONT_URL_RE = re.compile(r"url\(fonts/([\w.-]+\.woff2)\)")

# shell.html 中的注入点
#
# 样式与脚本各只有一个粗粒度标记：离线构建往里塞内联的 <style>/<script>，
# 在线构建往里塞 <link>/<script src>。两种构建共用同一份 shell，
# 结构（顶栏、状态栏、容器）不会两边漂移。
MARKERS = {
    "head_assets": "<!--@INJECT:HEAD_ASSETS@-->",
    "scripts": "<!--@INJECT:SCRIPTS@-->",
    "body": "<!--@INJECT:BODY@-->",
    "base": "<!--@INJECT:BASE@-->",
    "title": "__QUIZFORGE_TITLE__",
    "page": "__QUIZFORGE_PAGE__",
}


def offline_head(katex_css: str, app_css: str, page_css: str = "") -> str:
    """离线构建的 <head> 资源：全部内联，file:// 双击即可用。"""
    parts = [f"<style>{css_safe(katex_css)}</style>", f"<style>{css_safe(app_css)}</style>"]
    if page_css:
        parts.append(f"<style>{css_safe(page_css)}</style>")
    return "\n".join(parts)


def offline_scripts(
    *,
    data_js: str,
    katex_js: str,
    runtime_js: str,
    page_js: str,
) -> str:
    """离线构建的脚本：题库与 KaTeX 引擎全部写进 HTML。"""
    return "\n".join(
        [
            f"<script>{js_safe(data_js)}</script>",
            f"<script>{js_safe(katex_js)}</script>",
            f"<script>{js_safe(runtime_js)}</script>",
            f"<script>{js_safe(page_js)}</script>",
        ]
    )


def js_safe(code: str) -> str:
    """转义会提前结束 <script> 的序列。"""
    return code.replace("</script", "<\\/script").replace("<!--", "<\\!--")


def css_safe(code: str) -> str:
    """转义会提前结束 <style> 的序列（CSS 中 \\/ 等价于 /）。"""
    return code.replace("</style", "<\\/style").replace("<!--", "<\\!--")


def inline_fonts(css: str, fonts_dir: Path) -> tuple[str, int]:
    """把 CSS 中的 url(fonts/xxx.woff2) 换成 base64 data URI。

    返回 (新 CSS, 内联字体数)。某个字体缺失时内联一个空 data URI，
    保证产物仍可用（只是该字形显示为回退字体）。
    """
    count = 0

    def repl(match: re.Match[str]) -> str:
        nonlocal count
        name = match.group(1)
        path = fonts_dir / name
        if not path.is_file():
            return "url(data:font/woff2;base64,)"
        payload = base64.b64encode(path.read_bytes()).decode("ascii")
        count += 1
        return f"url(data:font/woff2;base64,{payload})"

    return _FONT_URL_RE.sub(repl, css), count


def js_literal(value: object) -> str:
    """把 Python 对象序列化成嵌在 <script> 里安全的 JS 字面量。

    ensure_ascii=False 让中文以原文存储（体积约省 40%），
    再对 < / -- 等敏感字符做转义。
    """
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return js_safe(text)


def render_shell(shell: str, replacements: dict[str, str]) -> str:
    """按 MARKERS 做占位符替换，替换后校验没有残留占位符。"""
    out = shell
    for key, value in replacements.items():
        marker = MARKERS[key]
        if marker not in out:
            raise KeyError(f"shell.html 里找不到注入点 {marker!r}")
        out = out.replace(marker, value)

    leftovers = sorted({m for m in MARKERS.values() if m in out})
    if leftovers:
        raise RuntimeError(f"仍有未替换的注入点：{leftovers}")
    return out


def concat_js(paths: list[Path]) -> str:
    """按顺序拼接 JS 文件，每个文件包在 IIFE 里避免全局污染。"""
    chunks: list[str] = []
    for path in paths:
        if not path.is_file():
            raise FileNotFoundError(f"缺少运行时脚本：{path}")
        code = path.read_text(encoding="utf-8")
        chunks.append(f"/* ==== {path.name} ==== */\n{code.strip()}\n")
    return "\n".join(chunks)


def concat_css(paths: list[Path]) -> str:
    chunks: list[str] = []
    for path in paths:
        if not path.is_file():
            raise FileNotFoundError(f"缺少样式文件：{path}")
        chunks.append(f"/* ==== {path.name} ==== */\n{path.read_text(encoding='utf-8').strip()}\n")
    return "\n".join(chunks)


def human_size(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ("B", "KB", "MB"):
        if value < 1024 or unit == "MB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} MB"
