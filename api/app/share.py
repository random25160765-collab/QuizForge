"""把一条会话装配成**一个能发出去的网页**（对话正文 + 那棵对话树，双击即用）。

## 为什么这里可以内联 —— 而"离线形态"当年被否掉

`docs/DESIGN.md` §二 记着那条决定：不做"全部内联进单个 HTML、双击即用"的离线形态。
理由不是"内联"本身，而是它**要求维护第二种应用形态**：题库内联、进度落 localStorage、
"服务端权威/本地乐观"两份状态要维持一致（§一）。那是一条关于**应用形态**的决定。

这里不是那件事：产出的是一件**只读产物** —— 一条会话 + 它的树，没有题库、没有进度、
没有写回、没有第二条数据通路。**但它继承了那条决定底下真正的原则：渲染器只能有一份源码。**
所以这里不重写任何渲染逻辑，只做装配：

  1. 把构建好的 `chat.html` **原样读进来** —— 页面结构、脚本顺序、样式，一个字符都不动；
  2. 把 `<link>` / `<script src>` 换成内联（`file://` 下 fetch 与 ES module 会被 CORS
     拦下，只有内联可用）；
  3. 塞进数据（`window.__QF_SHARE__`）与引导（`assets/runtime/share.js`）；
  4. **丢掉 `boot.js`** —— 它会先拉题库（`/bank`），空库直接中止在"题库还没有题目"那一屏，
     而分享页根本没有题库这回事。

于是**在线那一份改了，分享页跟着改** —— 没有第二份界面代码会长歪。

（同一个装配器有两个入口：`tools/share.py` 是命令行，导出接口是 `format=html`。）
"""
from __future__ import annotations

import base64
import json
import re
from html.parser import HTMLParser
from pathlib import Path

#: 分享页自己的样式：只做一件事 —— 把**输入区收起来**。
#: 留着它就是个"能打字但发不出去"的陷阱；读写能力是这一页没有的东西，
#: 那就不该在界面上假装有。其余布局一律照在线那一份。
SHARE_CSS = """
html[data-share] .chat__composer { display: none; }
html[data-share] .chat__notice:empty { display: none; }
"""

LINK = re.compile(r'<link rel="stylesheet" href="([^"]+)"\s*/?>')
SCRIPT = re.compile(r'<script src="([^"]+)"[^>]*></script>')


class ShareBuildError(RuntimeError):
    """装配自检没过。

    **不要降级成一个"能下载但打开是白的"文件** —— 那比当场报错难查得多：
    文件大小正常、浏览器也不报错，人只会以为是自己那边的问题。
    """

    def __init__(self, problems: list[str]) -> None:
        super().__init__("；".join(problems))
        self.problems = problems


class _Blocks(HTMLParser):
    """按**真正的 HTML 规则**切出内联脚本（带 `src` 的不算）。

    为什么不自己写正则切：翻车过一次，正是"以为 `</script>` 一定结束脚本"。
    HTML 在脚本内容里遇到 `<!--` 之后再遇到 `<script`，会进 **double-escaped**
    状态 —— 那时 `</script>` **不结束**脚本，得等一个 `-->`。所以"脚本边界在哪"
    不能靠猜，交给标准库的解析器（它实现的就是浏览器那套状态机）。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.blocks: list[str] = []
        self._buf: list[str] | None = None

    def handle_starttag(self, tag, attrs):  # noqa: ANN001
        if tag == "script" and not dict(attrs).get("src"):
            self._buf = []

    def handle_endtag(self, tag):  # noqa: ANN001
        if tag == "script" and self._buf is not None:
            self.blocks.append("".join(self._buf))
            self._buf = None

    def handle_data(self, data):  # noqa: ANN001
        if self._buf is not None:
            self._buf.append(data)


def _safe_for_script(text: str) -> str:
    """让一段 JS 能安全地内联进 `<script>` 里。

    **HTML 解析器只认 `</script`**（大小写不敏感），它不认 JS 的字符串边界 ——
    只要源码里出现一次，这段内联脚本就在**那里**被提前结束，后面的代码全变成
    HTML 文本（实测：分享页白屏、`QF.chat` 根本没被定义）。
    换掉之后语义等价：`<\\/` 在 JS 字符串里就是 `</`，而它在字符串之外本来就不可能出现。
    """
    return re.sub(r"</(script)", r"<\\/\1", text, flags=re.IGNORECASE)


def _json_for_script(payload: str) -> str:
    """让一段 JSON 能安全地内联进 `<script>` 里。

    同一个坑的另一半，而且**数据这一侧更容易踩**：会话里 `demo` 零件的 `html`
    就是整份 HTML 文档，里面必然有 `</script>`。把 `<` 转成 `\\u003c`
    （JSON 本来就允许这种转义，解析回来一模一样）就挡掉了 —— 顺带也挡掉 `<!--`
    （它会和随后的 `<script` 一起把解析器带进 double-escaped 状态）。
    """
    return payload.replace("<", "\\u003c")


def _css_with_fonts(css: str, base_dir: Path) -> str:
    """把 `url(fonts/x.woff2)` 换成一串 base64。

    字体是**唯一**必须 base64 的东西（图片这类不进分享页）。KaTeX 二十来个字形子集
    加起来 ~300KB，转 base64 涨三分之一 —— 换来的是一页里公式不会退化成纯文本。
    """

    def one(match: re.Match) -> str:
        ref = match.group(1).strip("'\"")
        if ref.startswith(("data:", "http:", "https:", "#")):
            return match.group(0)
        target = (base_dir / ref).resolve()
        if not target.is_file():
            return match.group(0)  # 找不到就原样留着：让它照常失败，别静默改路径
        mime = "font/woff2" if target.suffix == ".woff2" else "application/octet-stream"
        blob = base64.b64encode(target.read_bytes()).decode("ascii")
        return "url(data:%s;base64,%s)" % (mime, blob)

    return re.sub(r"url\(([^)]+)\)", one, css)


def _verify(html: str, stats: dict) -> list[str]:
    """装配完**自己检一遍**，返回问题清单（空 = 过）。

    为什么非有这一步：这类错误的表现是"**页面白屏，而文件看着是完整的**" ——
    大小正常、浏览器也不报错。实测为它查了很久（根因是 `</body>` 在页面里
    不止一处，一次 `replace` 把数据插进了某段 JS 中间，见 `_inline` 末尾）。

    只查几条**一旦违反就必然白屏**的不变量，不追求穷尽。
    """
    problems: list[str] = []
    parser = _Blocks()
    parser.feed(html)
    blocks = parser.blocks

    # 期望块数 = 源页面本来就有的内联脚本（shell 的首帧布局那一段）+ 内联进来的
    # 各脚本 + 数据块 + 引导块。**不写死数字** —— 页面将来多一段内联脚本，
    # 这里就该跟着变，而不是报一个假警。
    want = stats.get("inline", 1) + stats["js"] + 2
    if len(blocks) != want:
        problems.append(
            "内联脚本块 %d 个，期望 %d 个 —— 多半有脚本被提前结束了" % (len(blocks), want)
        )

    payload = [b for b in blocks if "window.__QF_SHARE__=" in b]
    if len(payload) != 1:
        problems.append("数据块出现 %d 次（应为 1）" % len(payload))
    elif not payload[0].lstrip().startswith("window.__QF_SHARE__="):
        problems.append("数据块里掺了别的东西 —— 它被插进了某段脚本中间")

    guide = [b for b in blocks if "单页分享页的引导" in b]
    if len(guide) != 1:
        problems.append("引导块出现 %d 次（应为 1）" % len(guide))

    chat = [b for b in blocks if "chat.js —— 对话" in b]
    if len(chat) != 1:
        problems.append("chat.js 块出现 %d 次（应为 1）" % len(chat))
    elif "__QF_SHARE__" in chat[0]:
        problems.append("数据被插进了 chat.js 里面")
    return problems


def _inline(page: str, share_js: str, payload: str, web_dir: Path) -> tuple[str, dict]:
    """把页面里所有外链资源换成内联，并换掉引导与数据。"""
    stats: dict = {"css": 0, "js": 0, "boot": 0}
    probe = _Blocks()
    probe.feed(page)
    stats["inline"] = len(probe.blocks)  # 源页面自带的内联脚本（shell 的首帧布局那一段）

    def _asset(path: str) -> Path:
        """把页面里的 `/assets/xxx?v=hash` 还原成磁盘上的文件。"""
        return web_dir / path.split("?")[0].lstrip("/")

    def css_tag(match: re.Match) -> str:
        target = _asset(match.group(1))
        if not target.is_file():
            return match.group(0)
        stats["css"] += 1
        return "<style>\n%s\n</style>" % _css_with_fonts(
            target.read_text(encoding="utf-8"), target.parent
        )

    def js_tag(match: re.Match) -> str:
        src = match.group(1)
        if src.endswith("boot.js") or "boot.js?" in src:
            # 见模块头第 4 条：boot 会去拉题库，分享页不需要它，也不能有它。
            stats["boot"] += 1
            return "<!-- boot.js 已换成 share.js（分享页不拉题库） -->"
        target = _asset(src)
        if not target.is_file():
            return match.group(0)
        stats["js"] += 1
        return "<script>\n%s\n</script>" % _safe_for_script(
            target.read_text(encoding="utf-8")
        )

    out = LINK.sub(css_tag, page)
    out = SCRIPT.sub(js_tag, out)
    out = out.replace('{"mode":"online"', '{"mode":"share"')

    tail = (
        "<script>window.__QF_SHARE__=%s;</script>\n"
        "<style>%s</style>\n"
        "<script>\n%s\n</script>\n"
        % (_json_for_script(payload), SHARE_CSS, _safe_for_script(share_js))
    )
    # **只插在最后一个 `</body>` 前面。**
    # 不能 `replace`：`</body>` 在页面里**不止一处** —— `chat.js` 那段演示套件的
    # 模板字符串里就写着 `'</head><body>' + html + '</body></html>'`。
    # 一次 `replace` 会把数据与引导插进那段 JS 的中间：脚本语法当场崩掉，
    # 而**文件看着是完整的**（实测白屏过一次，查了很久）。
    cut = out.rindex("</body>")
    return out[:cut] + tail + out[cut:], stats


def payload_of(conv, messages: list) -> dict:  # noqa: ANN001
    """给前端的形状与 `GET /chat/conversations/{id}` **一模一样** ——
    这样 `chat.js` 那一边一行都不用改（它本来就在等这个形状）。"""
    return {
        "conversation": {
            "id": str(conv.id),
            "title": conv.title or "",
            "folder": conv.folder or "",
            "createdAt": conv.created_at.isoformat() if conv.created_at else "",
            "updatedAt": conv.updated_at.isoformat() if conv.updated_at else "",
        },
        "messages": messages,
    }


def build_single_page(web_dir: Path, payload: dict) -> tuple[str, dict]:
    """装配成一个自包含的 HTML。返回 `(html, 统计)`。

    数据由调用方给（接口那边是 `_message_out` 过的列表），这里只管装配 ——
    于是命令行与接口走的是**同一条路**，不会各自长歪。

    **自检不过就抛 `ShareBuildError`**，不返回半成品：一个"能下载、打开是白的"
    文件比一条报错难查得多（实测为它查了很久）。
    """
    page_path = web_dir / "chat.html"
    share_js_path = web_dir / "assets" / "runtime" / "share.js"
    if not page_path.is_file():
        raise FileNotFoundError("没有前端产物：先跑 `make web`（缺 %s）" % page_path)
    if not share_js_path.is_file():
        raise FileNotFoundError("缺分享引导脚本：%s（`make web` 会把它拷进产物）" % share_js_path)

    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    html, stats = _inline(
        page_path.read_text(encoding="utf-8"),
        share_js_path.read_text(encoding="utf-8"),
        body,
        web_dir,
    )
    problems = _verify(html, stats)
    if problems:
        raise ShareBuildError(problems)
    return html, stats


__all__ = ["SHARE_CSS", "ShareBuildError", "build_single_page", "payload_of"]
