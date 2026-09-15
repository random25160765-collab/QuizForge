#!/usr/bin/env python3
"""在线模式的前端构建。

与 `build.py` 的离线产物是同一份源码、同一份 shell，区别只在「资源怎么给」：

  离线（dist/）      把 CSS/JS/KaTeX/题库全部内联进单个 HTML，双击即用
  在线（api/web/）   页面 + /assets 静态资源，数据来自 /api

刻意复用 `theme/shell.html`，不另起一份模板：顶栏、状态栏这些结构
一旦有两份拷贝，两边迟早会长得不一样。

产物布局（与 app/config.py 的 web_dir、main.py 的挂载点对应）::

    api/web/
    ├── index.html      首页（入口指向登录/刷题）
    ├── login.html      登录 / 注册
    ├── quiz.html       刷题应用
    ├── wrongbook.html  错题本
    └── assets/
        ├── app.css  katex.css  katex.min.js
        ├── fonts/*.woff2
        └── runtime/*.js
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import inline as _inline

ROOT = Path(__file__).resolve().parent.parent
THEME_DIR = ROOT / "theme"
RUNTIME_DIR = THEME_DIR / "runtime"
PAGES_DIR = THEME_DIR / "pages"
VENDOR_KATEX = ROOT / "vendor" / "katex"

# 与 build.py 保持一致：运行时脚本按依赖顺序加载
RUNTIME_ORDER = [
    "ui.js",
    "api.js",
    "data.js",
    "md.js",
    "highlight.js",
    "engine.js",
    "store.js",
    "sync.js",
    "sm2.js",
    "ai.js",
    "qview.js",
    "router.js",
]

# 每个页面额外加载的脚本；boot.js 统一放在最后（它要调用页面的 boot）
PAGE_JS = {
    "quiz": ["app.js", "boot.js"],
    "wrongbook": ["wrongbook.js", "boot.js"],
}

PAGE_TITLE = {
    "quiz": "quizforge · 刷题",
    "wrongbook": "quizforge · 错题本",
}

PAGE_BODY = {
    "quiz": "quiz.body.html",
    "wrongbook": "wrongbook.body.html",
}

# 应用样式（离线构建按页面拼，这里合并成一份，避免每页重复下载）
APP_CSS = ["markdown.css", "app.css"]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _digest(path: Path) -> str:
    """内容指纹，用于给静态资源加 ?v= 查询串。

    没有指纹时浏览器会把旧脚本缓存到过期，改完代码刷新却看不到变化 ——
    这是本地迭代与线上发版都会踩的坑。加了之后内容一变 URL 就变，
    同时可以放心地把 Cache-Control 拉长。
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()[:8]


def _asset(rel: str, digest: str) -> str:
    return f"/assets/{rel}?v={digest}"


def _config_script(api_base: str, page: str) -> str:
    payload = {"mode": "online", "apiBase": api_base, "page": page}
    return f"<script>window.QF_CONFIG={json.dumps(payload, separators=(',', ':'))};</script>"


def build(out_dir: Path, log, *, api_base: str = "/api") -> dict:
    """输出在线模式的前端产物，返回统计信息。"""
    assets = out_dir / "assets"
    runtime_out = assets / "runtime"
    fonts_out = assets / "fonts"
    for directory in (out_dir, assets, runtime_out, fonts_out):
        directory.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------- KaTeX
    katex_css_raw = _read(VENDOR_KATEX / "katex.css")
    (assets / "katex.css").write_text(katex_css_raw, encoding="utf-8")
    shutil.copy2(VENDOR_KATEX / "katex.min.js", assets / "katex.min.js")

    # 字体单独落地：katex.css 里写的是 url(fonts/xxx.woff2)，相对路径依旧成立。
    # 离线构建把 20 个 woff2 转 base64 内联（约 400KB），在线模式只下载用到的字形子集。
    font_count = 0
    for src in sorted((VENDOR_KATEX / "fonts").glob("*.woff2")):
        shutil.copy2(src, fonts_out / src.name)
        font_count += 1

    # ------------------------------------------------------ 应用样式
    (assets / "app.css").write_text(
        _inline.concat_css([THEME_DIR / name for name in APP_CSS]), encoding="utf-8"
    )

    # ---------------------------------------------------- 运行时脚本
    # 页面级脚本也要一起落地：它们和运行时脚本一样以 <script src> 引入，
    # 漏拷会导致页面 404 白屏（曾经就漏过 boot.js 与 app.js）
    scripts_to_copy = list(RUNTIME_ORDER)
    for page_js in PAGE_JS.values():
        for name in page_js:
            if name not in scripts_to_copy:
                scripts_to_copy.append(name)

    for name in scripts_to_copy:
        src = RUNTIME_DIR / name
        if not src.is_file():
            raise FileNotFoundError(f"缺少运行时脚本：{src}")
        shutil.copy2(src, runtime_out / name)

    # ---------------------------------------------------------- 页面
    shell = _read(THEME_DIR / "shell.html")
    pages_written = []

    katex_css_url = _asset("katex.css", _digest(assets / "katex.css"))
    app_css_url = _asset("app.css", _digest(assets / "app.css"))
    katex_js_url = _asset("katex.min.js", _digest(assets / "katex.min.js"))
    runtime_url = {name: _asset(f"runtime/{name}", _digest(runtime_out / name)) for name in scripts_to_copy}

    for page, page_js in PAGE_JS.items():
        head_assets = "\n".join(
            [
                f'<link rel="stylesheet" href="{katex_css_url}">',
                f'<link rel="stylesheet" href="{app_css_url}">',
            ]
        )
        scripts = [
            _config_script(api_base, page),
            f'<script src="{katex_js_url}"></script>',
        ]
        # KaTeX 必须先于运行时：md.js 在渲染时就调用 window.katex
        scripts += [f'<script src="{runtime_url[name]}"></script>' for name in RUNTIME_ORDER]
        scripts += [f'<script src="{runtime_url[name]}"></script>' for name in page_js]

        html = _inline.render_shell(
            shell,
            {
                "title": PAGE_TITLE[page],
                "page": page,
                "base": "",
                "head_assets": head_assets,
                "scripts": "\n".join(scripts),
                "body": _read(PAGES_DIR / PAGE_BODY[page]),
            },
        )
        (out_dir / f"{page}.html").write_text(html, encoding="utf-8")
        pages_written.append(page)

    # -------------------------------------------------- 登录页 / 首页
    login_html = _read(THEME_DIR / "login.html").replace(
        "</head>", f"{_config_script(api_base, 'auth')}\n</head>", 1
    )
    (out_dir / "login.html").write_text(login_html, encoding="utf-8")
    pages_written.append("login")

    # 首页的规模数字改由页面自己去 /api/health 取 —— 在线部署下
    # 「上次构建时的题数」已经是过期信息，静态写死会误导
    landing_html = (
        _read(THEME_DIR / "landing.html")
        .replace("__LANDING_TITLE__", "quizforge")
        .replace("__LANDING_TOTAL__", "—")
        .replace("__LANDING_TOPICS_N__", "—")
        .replace("__LANDING_TYPES_N__", "—")
        .replace("__LANDING_GROUP_N__", "—")
        .replace("__LANDING_GENERATED__", "按需加载")
        # 离线首页用内联的题库 id 列表统计错题；在线模式不需要这段
        .replace("__LANDING_IDS__", "[]")
        # 覆盖范围同样由页面自己去 /api/health 取（服务端有真实主题树，
        # 构建期把一份快照写死只会在改题后显示过期信息）。
        # 这里先留空占位，脚本拿到数据后填充；取不到就是空列表，不影响其余内容。
        .replace("__LANDING_SUBJECTS__", "")
        .replace("</head>", f"{_config_script(api_base, 'landing')}\n</head>", 1)
    )
    (out_dir / "index.html").write_text(landing_html, encoding="utf-8")
    pages_written.append("index")

    info = {
        "pages": pages_written,
        "runtime": len(RUNTIME_ORDER),
        "fonts": font_count,
        "out": out_dir,
    }
    log.step(
        f"在线前端：{len(pages_written)} 个页面 · {len(RUNTIME_ORDER)} 个运行时脚本 · "
        f"{font_count} 个字体 -> {out_dir}"
    )
    return info
