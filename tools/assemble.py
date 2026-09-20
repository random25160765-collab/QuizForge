#!/usr/bin/env python3
"""把页面外壳与样式装配成在线产物（`api/web/`）。

历史上这个模块叫 `inline.py`：那时有两种形态，离线版要把 CSS / JS / KaTeX / 题库
全部内联进单个 HTML（`file://` 下 fetch 与 ES module import 会被 CORS 拦下，
只有内联可用）。**离线形态已淘汰**，现在只有在线一种形态：页面走 `/assets`
静态资源、数据走 `/api`，所以这里只剩两件事——

  1. 按注入点装配 `theme/shell.html` 成页面
  2. 把多份样式合成一份（避免每页重复下载）

注入点是 shell 里的粗粒度标记：样式与脚本各一个。刷题页与错题本共用同一份外壳，
结构（顶栏、状态栏、容器）不会两边漂移。
"""

from __future__ import annotations

from pathlib import Path

# shell.html 中的注入点
MARKERS = {
    "head_assets": "<!--@INJECT:HEAD_ASSETS@-->",
    "scripts": "<!--@INJECT:SCRIPTS@-->",
    "body": "<!--@INJECT:BODY@-->",
    "base": "<!--@INJECT:BASE@-->",
    "title": "__QUIZFORGE_TITLE__",
    "page": "__QUIZFORGE_PAGE__",
    # 首帧就该定下来的两个布局属性（过去由 JS 隔 50~150ms 才设，切页时会
    # 看到内容整体位移一条顶栏的高度）。值见 build_web.py 的两张表。
    "topbar": "__QUIZFORGE_TOPBAR__",
    "statusbar": "__QUIZFORGE_STATUSBAR__",
}


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


def concat_css(paths: list[Path]) -> str:
    chunks: list[str] = []
    for path in paths:
        if not path.is_file():
            raise FileNotFoundError(f"缺少样式文件：{path}")
        chunks.append(f"/* ==== {path.name} ==== */\n{path.read_text(encoding='utf-8').strip()}\n")
    return "\n".join(chunks)
