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
    "docview.js",       # 一份文件怎么看（PDF / 图片 / Markdown / Word / 幻灯片）：资料页与窗格共用
    "data.js",
    "md.js",
    "highlight.js",
    "engine.js",
    "store.js",
    "sync.js",
    "sm2.js",
    "ai.js",
    "shell.js",
    "notegraph.js",     # 笔记双链图谱：资源页的窗格与笔记页的标签共用（见 theme/runtime/notegraph.js）
    "panes.js",         # 可组合窗格（tmux 式）：布局树、标签、拆分、拖拽比例、最大化；样式见 theme/pane.css
    "sidetree.js",      # 左栏资源树：对话 / 资料 / 文档三个根，点或拖都把东西送进右边的窗格
    "mounts.js",        # 顶栏的挂载开关：亮度＝AI 能调哪几块
    "qview.js",
    "router.js",
]

#: 拷进产物，但**不挂进任何页面**的脚本。
#: 目前只有 `share.js`（单页分享的引导）：它由 `tools/share.py` 与导出接口在装配
#: 那一页时内联，在线页面一律不引 —— 它一跑就 `QF.chat.boot()`，挂别处会撞车。
COPIED_ONLY_JS = ["share.js"]

# 每个页面额外加载的脚本；boot.js 统一放在最后（它要调用页面的 boot）
# 图谱页不需要 boot.js：它的数据来自公开的 /api/graph，自己启动
PAGE_JS = {
    "quiz": ["app.js", "boot.js"],
    "wrongbook": ["wrongbook.js", "boot.js"],
    "graph": ["graph.js"],
    "chat": ["chat.js", "boot.js"],
    "notes": ["canvas.js", "notes.js"],  # canvas.js 要在 notes.js 前（后者用 QF.canvas）
    "library": ["library.js"],           # 资料页也自己启动（数据来自 /api/library）
    # 工作台：窗格引擎 + 视图注册（canvas 先于 workbench，后者用 QF.canvas）。
    # chat.js 也在这儿：对话是一个**窗格种类**（点左栏的对话就地看，不跳页）。
    # 它自带 `QF.chat.boot`，但只有 chat.html 会去调 —— 这一页只用到 `QF.chat.mount`。
    # notes.js 也在：主页的笔记窗格要用它那套渲染零件（`QF.notesrt`）。
    # 它自带自启动，但只有 notes.html 会真的启动（见文件末尾的 bootPage 守卫）。
    "workbench": ["canvas.js", "chat.js", "notes.js", "workbench.js"],
}

#: 首帧就该是最终态的两个布局属性（见 shell.html 里的说明）：顶栏与底部状态栏
#: 占不占位。过去这两个由 JS 在 50~150ms 后才设，于是首帧与最终布局差一条顶栏 ——
#: 切页时看到的就是"里面的元素位移一下 + 闪"。
#: 值来自逐页实测（2026-09-20）：三页有子导航（所以有顶栏）；只有工作台有资源栏
#: 与状态栏。缺省一律 off —— 与"没有这条栏"的最终态一致。
PAGE_TOPBAR = {"quiz": "on", "wrongbook": "on", "graph": "on"}
PAGE_STATUSBAR = {"workbench": "on"}

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
#
# 工作台也要 chat.css：对话是一个**窗格种类**（`QF.panes.register('chat')`，
# 见 runtime/workbench.js），不带上这一份，那一格里的对话就是**没穿衣服**的
# ——会话列表铺成一长条、消息流不滚（用户报的"主页的对话显示不正常"）。
# 那份样式里除五条 `body[data-page='chat']` 的整页布局外全是类名作用域
# （`.chat*` / `.hub*` / `.chatlist*` / `.chatmsg*` / `.ct*`），不会漏到别的页；
# 窗格那一份布局在 chat.css 里另有一条 `.panes__host > .chat`。
PAGE_CSS = {
    "graph": ["graph.css"],
    "chat": ["chat.css"],
    "notes": ["notes.css"],
    "library": ["library.css"],
    "workbench": ["notes.css", "pane.css", "chat.css"],
}

# 应用样式合并成一份，避免每页重复下载
# 文件查看器的样式：资料页与工作台都要用，所以进共用那一份
APP_CSS = ["markdown.css", "app.css", "docview.css"]


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


def _script_tag(url: str) -> str:
    """一个 `<script src>` 标签，**一律带 defer**。

    为什么：一页要引二十来个脚本，而且**必须按顺序执行**（KaTeX → 运行时 → 页面）。
    原先是不带 defer 的同步标签 —— 浏览器解析到它们就停下等下载与执行，首帧被
    推迟到全部跑完（实测 DOMReady 57~99ms，那期间屏幕是白的）。

    加 defer 之后：**下载与解析并行、执行仍按文档顺序**，首帧可以先画出外壳
    （顶栏、左栏、骨架），脚本随后就绪。用户看到的不再是"白屏一下"，而是
    "界面在那儿、内容马上来"—— 这与"页面切换不够丝滑"是同一件事：整页跳转
    本来就快（TTFB 3ms），慢的就是这一段等待。
    """
    return f'<script src="{url}" defer></script>'


def _config_script(api_base: str, page: str) -> str:
    payload = {"mode": "online", "apiBase": api_base, "page": page}
    return f"<script>window.QF_CONFIG={json.dumps(payload, separators=(',', ':'))};</script>"


def _index_redirect() -> str:
    """首页：直接送进应用。

    单机形态下没有登录页、也没有"去登录还是去刷题"的岔路，
    所以首页只需要一件事：把浏览器送到应用里 —— 送到**对话**页。

    **为什么是对话而不是刷题**：这个项目的门面是"对话是前台，题与图是后台"
    （见 README 与 `docs/THESIS.md`）—— 问、讲、做题、记笔记都发生在同一个
    上下文里，其余页面是它的侧门。这里原先跳 `quiz.html`：那时题库是中心，
    后来前台换成了对话，这条跳转没跟着改 —— 于是"打开首页看到刷题页"
    与门面宣言矛盾（2026-09-22 实测发现，同一处矛盾在三处各说各话：
    这里跳 `quiz.html`、`make api-dev` 提示 `notes.html`、README 说对话是主入口）。

    用 `meta refresh` 而不是 JS 跳转 —— 不依赖脚本能不能跑起来。
    """
    return (
        "<!DOCTYPE html>\n"
        '<html lang="zh-CN">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta http-equiv="refresh" content="0; url=./chat.html">\n'
        "<title>QuizForge</title>\n"
        "</head>\n"
        "<body>\n"
        '<p>正在进入 <a href="./chat.html">QuizForge</a>…</p>\n'
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

    # CodeMirror 6 内核（见 `vendor/cm6/` 与 `tools/build_cm6.mjs`）。
    # 它是**构建期打好的产物**（CM6 是 ESM-only，前端没有打包链），拷进来即可；
    # 缺失也能构建 —— 只是笔记页的编辑器用不了（与 hljs 同一条规矩：可选资源不挡构建）。
    vendor_cm6 = ROOT / "vendor" / "cm6" / "cm6.js"
    if vendor_cm6.is_file():
        shutil.copy2(vendor_cm6, assets / "cm6.js")

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
    # 拷进产物、但**不挂进任何页面**的脚本。
    # `share.js` 是单页分享的引导：`make share` / 导出接口装配那一页时会把它内联进去，
    # 而在线页面**一律不许引它** —— 它一跑就 `QF.chat.boot()`，挂在别的页上会当场撞车。
    # 所以它不能进 `RUNTIME_ORDER`（那是"每一页都加载"的意思），只能走这里。
    for name in COPIED_ONLY_JS:
        if name not in scripts_to_copy:
            scripts_to_copy.append(name)
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
    cm6_path = assets / "cm6.js"
    cm6_js_url = _asset("cm6.js", _digest(cm6_path)) if cm6_path.is_file() else ""
    runtime_url = {name: _asset(f"runtime/{name}", _digest(runtime_out / name)) for name in scripts_to_copy}

    page_stamps: dict[str, str] = {}
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
            _script_tag(katex_js_url),
            # 只有笔记页要编辑器内核（577KB，别让别的页也背）
            *([_script_tag(cm6_js_url)] if (page == "notes" and cm6_js_url) else []),
        ]
        # hljs 也要排在运行时之前：`QF.highlight` 在渲染代码块时就问它有没有到位
        if hljs_js_url:
            scripts.append(_script_tag(hljs_js_url))
        # KaTeX 必须先于运行时：md.js 在渲染时就调用 window.katex
        scripts += [_script_tag(runtime_url[name]) for name in RUNTIME_ORDER]
        scripts += [_script_tag(runtime_url[name]) for name in page_js]

        html = _assemble.render_shell(
            shell,
            {
                "title": PAGE_TITLE[page],
                "page": page,
                "topbar": PAGE_TOPBAR.get(page, "off"),
                "statusbar": PAGE_STATUSBAR.get(page, "off"),
                "base": "",
                "head_assets": head_assets,
                "scripts": "\n".join(scripts),
                "body": _read(PAGES_DIR / PAGE_BODY[page]),
            },
        )
        # 「我看到的这一版是哪一版」：把**这一页渲染出来的内容**的指纹写进 head，
        # 前端拿它跟服务端 `build.json` 里的值对 —— 不一样就说明产物换了，自己刷新。
        # 指纹取的是注入**之前**的内容：注进去的那 12 个字符不该参与自我指涉。
        page_digest = hashlib.sha256(html.encode("utf-8")).hexdigest()[:12]
        assert "</head>" in html, "shell 模板里没有 </head>，注入点没了"
        html = html.replace("</head>", f'<meta name="qf-build" content="{page_digest}">\n</head>', 1)
        page_stamps[page] = page_digest
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
                # 每一页各自的指纹：前端轮询 `/api/build` 比它，判断自己是不是旧的那一份
                "pageStamps": page_stamps,
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
