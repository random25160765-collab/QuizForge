#!/usr/bin/env python3
"""前端构建（在线是唯一形态）。

产物是「页面 + `/assets` 静态资源」，数据来自 `/api`：CSS / JS / KaTeX 都是普通文件，
浏览器按需下载并按内容指纹（`?v=`）缓存，不再有任何内联。

刻意复用 `theme/shell.html`，不另起一份模板：顶栏、状态栏这些结构
一旦有两份拷贝，两边迟早会长得不一样。

产物布局（与 app/config.py 的 web_dir、main.py 的挂载点对应）::

    api/web/
    ├── index.html      首页（直接送进应用；单用户本地形态没有登录页）
    ├── quiz.html       刷题应用
    ├── wrongbook.html  错题本
    └── assets/
        ├── app.css  katex.css  katex.min.js
        ├── fonts/*.woff2
        └── runtime/*.js
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

import assemble as _assemble


class Log:
    """极简日志：`step` 带箭头前缀、用于阶段，其余三种直说。"""

    def __init__(self, quiet: bool = False) -> None:
        self.quiet = quiet

    def step(self, message: str) -> None:
        if not self.quiet:
            print(f"[INFO] → {message}")

    def info(self, message: str) -> None:
        if not self.quiet:
            print(f"[INFO] {message}")

    def warn(self, message: str) -> None:
        print(f"[WARN] {message}")

    def error(self, message: str) -> None:
        print(f"[ERROR] {message}", file=sys.stderr)

ROOT = Path(__file__).resolve().parent.parent
THEME_DIR = ROOT / "theme"
RUNTIME_DIR = THEME_DIR / "runtime"
PAGES_DIR = THEME_DIR / "pages"
VENDOR_KATEX = ROOT / "vendor" / "katex"
# Pyodide：对话里「跑 Python」的运行时（可选，`make vendor` 取回）
VENDOR_PYODIDE = ROOT / "vendor" / "pyodide"
# 演示沙箱的前端套件：第三方（vendor/demo-kit）+ 我们自己那层（theme/demo-kit）
VENDOR_DEMO_KIT = ROOT / "vendor" / "demo-kit"
THEME_DEMO_KIT = ROOT / "theme" / "demo-kit"
# 代码排版：真等宽字体（进 assets/fonts/）+ highlight.js（进 assets/）
VENDOR_MONO = ROOT / "vendor" / "mono"
VENDOR_HLJS = ROOT / "vendor" / "hljs"

# 运行时脚本按依赖顺序加载；**新增脚本要登记在这里**，顺序错会引用到未定义的模块
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
    "shell.js",
    "panes.js",         # 可组合窗格（tmux 式）：布局树、拆分、拖拽比例、最大化；样式见 theme/pane.css
    "mounts.js",        # 顶栏的挂载开关：亮度＝AI 能调哪几块
    "qview.js",
    "router.js",
]

# 每个页面额外加载的脚本；boot.js 统一放在最后（它要调用页面的 boot）
# 图谱页不需要 boot.js：它的数据来自公开的 /api/graph，自己启动
PAGE_JS = {
    "quiz": ["app.js", "boot.js"],
    "wrongbook": ["wrongbook.js", "boot.js"],
    "graph": ["graph.js"],
    "chat": ["chat.js", "boot.js"],
    "notes": ["canvas.js", "notes.js"],  # canvas.js 要在 notes.js 前（后者用 QF.canvas）
    "library": ["library.js"],           # 资料页也自己启动（数据来自 /api/library）
    "workbench": ["canvas.js", "workbench.js"],  # 工作台：窗格引擎 + 视图注册（canvas 先于 workbench，后者用 QF.canvas）
}

PAGE_TITLE = {
    "quiz": "QuizForge · 刷题",
    "wrongbook": "QuizForge · 错题本",
    "graph": "QuizForge · 知识图谱",
    "chat": "QuizForge · 对话",
    "notes": "QuizForge · 笔记",
    "library": "QuizForge · 资料",
    "workbench": "QuizForge · 工作台",
}

PAGE_BODY = {
    "quiz": "quiz.body.html",
    "wrongbook": "wrongbook.body.html",
    "graph": "graph.body.html",
    "chat": "chat.body.html",
    "notes": "notes.body.html",
    "library": "library.body.html",
    "workbench": "workbench.body.html",
}

# 页面专属样式：默认共用合并后的 app.css，只有图谱页、对话页与笔记页要再加一份
# 工作台暂时带上 notes.css：画布那棵 DOM（`.ncanvas*`）的样式写在里面，
# 等画布样式从 notes.css 里拆出来之后，这一行就该瘦回去
PAGE_CSS = {
    "graph": ["graph.css"],
    "chat": ["chat.css"],
    "notes": ["notes.css"],
    "library": ["library.css"],
    "workbench": ["notes.css", "pane.css"],
}

# 应用样式合并成一份，避免每页重复下载
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


def _index_redirect() -> str:
    """首页：直接送进应用。

    单用户本地形态下没有登录页、也没有"去登录还是去刷题"的岔路，
    所以首页只需要一件事：把浏览器送到应用里。
    用 `meta refresh` 而不是 JS 跳转 —— 不依赖脚本能不能跑起来。
    """
    return (
        "<!DOCTYPE html>\n"
        '<html lang="zh-CN">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta http-equiv="refresh" content="0; url=./quiz.html">\n'
        "<title>QuizForge</title>\n"
        "</head>\n"
        "<body>\n"
        '<p>正在进入 <a href="./quiz.html">QuizForge</a>…</p>\n'
        "</body>\n"
        "</html>\n"
    )


def _fingerprint(out_dir: Path) -> dict:
    """给这次构建打一个戳：**前端产物 + 后端源码**一起算。

    为什么两边都算：用户提过"开发端有、exe 里没有"—— 那是两次构建内容不同，而当时
    没有任何地方看得出来。有了戳：一样 = 你看的界面和那个包是同一份东西；
    不一样 = 当场就能发现，而不是等用户找不到入口才回头查。

    `build.json` 自己不参与（否则戳会自我指涉、永远不稳定）。
    """
    digest = hashlib.sha256()
    web_files = 0
    for path in sorted(out_dir.rglob("*")):
        if not path.is_file() or path.name == "build.json":
            continue
        digest.update(path.relative_to(out_dir).as_posix().encode())
        digest.update(path.read_bytes())
        web_files += 1
    backend_files = 0
    app_dir = ROOT / "api" / "app"
    for path in sorted(app_dir.rglob("*.py")):
        digest.update(path.relative_to(app_dir).as_posix().encode())
        digest.update(path.read_bytes())
        backend_files += 1
    return {
        "stamp": digest.hexdigest()[:12],
        "webFiles": web_files,
        "backendFiles": backend_files,
    }


def build(out_dir: Path, log, *, api_base: str = "/api", with_pyodide: bool = False) -> dict:
    """输出在线模式的前端产物，返回统计信息。"""
    assets = out_dir / "assets"
    runtime_out = assets / "runtime"
    fonts_out = assets / "fonts"
    for directory in (out_dir, assets, runtime_out, fonts_out):
        directory.mkdir(parents=True, exist_ok=True)

    # 先清掉上一轮写下的页面：页面是**顶层 html**，删掉一个模板（比如随"去账号"
    # 一起删的 login.html）之后，旧产物会留在 out_dir 里继续被访问到 ——
    # 实测踩过：`/login.html` 还能打开，而后端已经没有登录这回事了。
    for stale in out_dir.glob("*.html"):
        stale.unlink()

    # ---------------------------------------------------------- KaTeX
    katex_css_raw = _read(VENDOR_KATEX / "katex.css")
    (assets / "katex.css").write_text(katex_css_raw, encoding="utf-8")
    shutil.copy2(VENDOR_KATEX / "katex.min.js", assets / "katex.min.js")

    # 字体单独落地：katex.css 里写的是 url(fonts/xxx.woff2)，相对路径依旧成立，
    # 浏览器只下载真正用到的字形子集。
    font_count = 0
    for src in sorted((VENDOR_KATEX / "fonts").glob("*.woff2")):
        shutil.copy2(src, fonts_out / src.name)
        font_count += 1

    # 正文字体：等宽那份（代码块用）。`--font-mono` 首选 JetBrains Mono，
    # 而本机没有它时浏览器只能退到 DejaVu Sans Mono —— 那份字在代码里很难看。
    for src in sorted(VENDOR_MONO.glob("*.woff2")) if VENDOR_MONO.is_dir() else []:
        shutil.copy2(src, fonts_out / src.name)
        font_count += 1

    # 语法高亮：到位就优先用它（手写那份留在 theme/runtime/highlight.js 兜底）
    hljs_out = assets / "hljs.min.js"
    hljs_ready = (VENDOR_HLJS / "highlight.min.js").is_file()
    if hljs_ready:
        shutil.copy2(VENDOR_HLJS / "highlight.min.js", hljs_out)

    # ---------------------------------------------------------- Pyodide
    # 对话里「跑 Python」用的运行时（**76M**）。**默认不拷**：
    #
    #   它不进安装包，改由 `app/heavy_deps.py` 在第一次打开时取一次进本机缓存
    #   （`data/cache/pyodide/`），之后离线可用。这样分发包只有 ~5M 静态资源，
    #   而不是每次都背上 76M —— 用户明确要求"保持轻量"。
    #
    # `--with-pyodide` 是给**离线开发/自包含部署**留的：那种情况下把运行时装进
    # 产物里，`heavy_deps` 会优先用本机副本、一次网都不联。
    pyodide_count = 0
    stale_pyodide = assets / "pyodide"
    if not with_pyodide and stale_pyodide.is_dir():
        # 上一次用 --with-pyodide 构建留下的 76M，这次不拷就得**删掉** ——
        # 否则产物看着是"轻量构建"，体积却一直是 81M（实测踩过：
        # 日志写着「无 Pyodide」，`du -sh api/web` 还是 81M）
        shutil.rmtree(stale_pyodide, ignore_errors=True)
        log.info("清掉上次留下的 assets/pyodide/（这次不打包运行时）")
    if with_pyodide and VENDOR_PYODIDE.is_dir():
        pyodide_out = assets / "pyodide"
        pyodide_out.mkdir(parents=True, exist_ok=True)
        for src in sorted(VENDOR_PYODIDE.iterdir()):
            if src.is_file() and src.name != "SOURCE.md":
                shutil.copy2(src, pyodide_out / src.name)
                pyodide_count += 1

    # ---------------------------------------------------------- 演示套件
    # 演示页跑在沙箱 iframe 里，库与样式由服务端注入（tools._demo_page）。
    # 三方库可选（没同步过就跳过），我们自己那层（theme/demo-kit）总是拷。
    kit_count = 0
    kit_out = assets / "demo-kit"
    for source_dir in (VENDOR_DEMO_KIT, THEME_DEMO_KIT):
        if not source_dir.is_dir():
            continue
        kit_out.mkdir(parents=True, exist_ok=True)
        for src in sorted(source_dir.iterdir()):
            if src.is_file() and src.name != "SOURCE.md":
                shutil.copy2(src, kit_out / src.name)
                kit_count += 1

    # ------------------------------------------------------ 应用样式
    (assets / "app.css").write_text(
        _assemble.concat_css([THEME_DIR / name for name in APP_CSS]), encoding="utf-8"
    )

    # 页面专属样式单独落地：图谱那 400 多行不该让每个页面都下载
    for page, names in PAGE_CSS.items():
        (assets / f"{page}.css").write_text(
            _assemble.concat_css([THEME_DIR / name for name in names]), encoding="utf-8"
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
    page_css_url = {
        page: _asset(f"{page}.css", _digest(assets / f"{page}.css")) for page in PAGE_CSS
    }
    katex_js_url = _asset("katex.min.js", _digest(assets / "katex.min.js"))
    hljs_js_url = _asset("hljs.min.js", _digest(hljs_out)) if hljs_ready else ""
    runtime_url = {name: _asset(f"runtime/{name}", _digest(runtime_out / name)) for name in scripts_to_copy}

    for page, page_js in PAGE_JS.items():
        links = [
            f'<link rel="stylesheet" href="{katex_css_url}">',
            f'<link rel="stylesheet" href="{app_css_url}">',
        ]
        if page in page_css_url:
            links.append(f'<link rel="stylesheet" href="{page_css_url[page]}">')
        head_assets = "\n".join(links)
        scripts = [
            _config_script(api_base, page),
            f'<script src="{katex_js_url}"></script>',
        ]
        # hljs 也要排在运行时之前：`QF.highlight` 在渲染代码块时就问它有没有到位
        if hljs_js_url:
            scripts.append(f'<script src="{hljs_js_url}"></script>')
        # KaTeX 必须先于运行时：md.js 在渲染时就调用 window.katex
        scripts += [f'<script src="{runtime_url[name]}"></script>' for name in RUNTIME_ORDER]
        scripts += [f'<script src="{runtime_url[name]}"></script>' for name in page_js]

        html = _assemble.render_shell(
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

    # -------------------------------------------------- 独立页
    # 启动页要在"什么都还没准备好"的时候就能显示，所以它**不走 shell**、
    # 也不 import app.css —— 由 theme/ 直接拷过去（不是渲染出来的那一类）。
    for name in ("starting.html", "starting.css"):
        source = THEME_DIR / name
        if source.is_file():
            shutil.copy2(source, out_dir / name)

    # -------------------------------------------------- 图标
    # favicon 与触屏图标放到 **web 根下**：页面都在根下，`<link href="icon.svg">`
    # 这类相对路径才解析得到（顶栏与对话头像用的是同一个字形的线条版，
    # 见 `theme/shell.html` 与 `chat.js` 的 `logoMark()`）。
    # 源文件由 `python3 tools/make_icons.py` 生成（纯标准库画的，没有图像库依赖）。
    icon_count = 0
    for name in ("icon.svg", "icon-256.png"):
        source = THEME_DIR / "assets" / name
        if source.is_file():
            shutil.copy2(source, out_dir / name)
            icon_count += 1

    # -------------------------------------------------- 首页
    # 原先这里构建两页：`login.html`（登录 / 注册）与 `index.html`（着陆页，由它决定
    # "去登录还是去刷题"）。单用户本地形态下两页都没有存在的理由 —— 没有账号要登，
    # 也没有岔路要选。首页现在只做一件事：把浏览器送进应用。
    # （真正的"首页"是三栏工作台，见 A 段。）
    (out_dir / "index.html").write_text(_index_redirect(), encoding="utf-8")
    pages_written.append("index")

    # ---------------------------------------------------------- 构建戳
    # 写进产物根下：后端 `/api/health` 会把它报出来，打包自检拿它比对 ——
    # "开发端看到的"和"包里那份"是不是同一份，从此有个可核对的数字。
    build_stamp = _fingerprint(out_dir)
    (out_dir / "build.json").write_text(
        json.dumps(
            {
                **build_stamp,
                "builtAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "pages": pages_written,
                "withPyodide": bool(pyodide_count),
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )

    info = {
        "pages": pages_written,
        "runtime": len(RUNTIME_ORDER),
        "fonts": font_count,
        "pyodide": pyodide_count,
        "demoKit": kit_count,
        "out": out_dir,
        "build": build_stamp,
    }
    log.step(
        f"在线前端：{len(pages_written)} 个页面 · {len(RUNTIME_ORDER)} 个运行时脚本 · "
        f"{font_count} 个字体"
        + (
            f" · Pyodide {pyodide_count} 个文件（自包含）"
            if pyodide_count
            else " · 不含 Pyodide（运行时首启取进本机缓存，见 app/heavy_deps.py）"
        )
        + (f" · 演示套件 {kit_count} 个文件" if kit_count else "")
        + f" · 构建戳 {build_stamp['stamp']}"
        + f" -> {out_dir}"
    )
    return info


def main(argv: list[str] | None = None) -> int:
    """命令行入口（`make web` 走这里）。

    构建入口原先在 `tools/build.py` —— 那是离线构建器，顺带代理在线构建。
    离线形态淘汰后只剩这一条路径，CLI 就跟着落在本模块。
    """
    parser = argparse.ArgumentParser(description="构建 quizforge 前端（在线形态）")
    parser.add_argument("--out", default=str(ROOT / "api" / "web"), help="输出目录，默认 api/web/")
    parser.add_argument("--api-base", default="/api", help="前端请求的接口前缀")
    parser.add_argument("--quiet", "-q", action="store_true", help="安静模式")
    parser.add_argument(
        "--with-pyodide",
        action="store_true",
        help="把 76M 的 Pyodide 运行时也拷进产物（离线自包含用；默认不拷，运行时首启取进缓存）",
    )
    args = parser.parse_args(argv)

    started = time.time()
    log = Log(quiet=args.quiet)
    out_dir = Path(args.out).resolve()

    # KaTeX 是页面渲染公式的硬依赖，缺了会静默退化成纯文本
    if not (VENDOR_KATEX / "katex.min.js").is_file():
        log.error("vendor/katex 未就绪，请先运行：make vendor")
        return 2

    info = build(out_dir, log, api_base=args.api_base, with_pyodide=args.with_pyodide)
    log.info(f"构建完成：{len(info['pages'])} 个页面 -> {out_dir}（{time.time() - started:.2f}s）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
