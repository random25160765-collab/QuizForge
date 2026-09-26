"""HTML → Markdown：网页这条支路的归一前端（`pipeline/normalize.py` 的 html 分支调它）。

**来源**：这份转换表借自 `~/Desktop/ANALYSIS/tools/html2md.py`（那次只做了公众号的 4 篇）。
其中值钱的三处**原样保留**：

* **公式** —— 公众号把 LaTeX 存在 `data-formula` 属性上（mdnice 的写法），还原成 `$…$` /
  `$$…$$`；丢了它，那几篇里的公式就全没了；
* **图片** —— `data-src` 优先、回退 `src`：懒加载的页面只有 `data-src` 里有真地址；
* **标题/列表/代码块/引用**的转换表。

那次的正文定位是**写死 `id="js_content"`**，找不到就整篇跳过（所以 29 个页面只出了 4 篇）。
这里泛化成「**每站一条规则 + 通用兜底**」：

* 规则按**内容特征**认，不靠文件名或目录名（保存下来的网页里，目录名是 `wiki`/`zartbot`
  这类站名，而域名往往只出现在页面内的 canonical/og:url 里）：
  公众号 `#js_content`、维基 `#mw-content-text`、`<article>`、`<main>`、`[role=main]`、
  `#content`/`.post-content`/`.article-content` 这些常见容器；
* 兜底不引第三方（环境里没有 bs4 / readability / lxml），用**三条能量的判据**打分：
  直接文本够长、**链接文本占比低**（导航栏与侧栏的典型长相就是"全是链接"）、文本块够多；
  取分最高者，并**向上合并同级的兄弟节点**（正文常被切在几个相邻 div 里）。

**边界**（用户确认过）：一个主 HTML = 一份材料；`*_files/` 里的一切（js/css/图片/子页面）
都算这份材料的资源，不进材料清单。
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from pathlib import Path

#: 这些标签整棵丢掉：脚本、样式、导航、页脚、表单、广告位。
DROP_TAGS = frozenset(
    {"script", "style", "noscript", "nav", "header", "footer", "aside", "form",
     "iframe", "svg", "button", "select", "textarea", "template"}
)
#: 这些标签自带换行（块级）。
BLOCK_TAGS = frozenset(
    {"p", "div", "section", "article", "main", "blockquote", "pre", "ul", "ol", "li",
     "h1", "h2", "h3", "h4", "h5", "h6", "table", "tr", "hr", "figure", "figcaption", "br"}
)
#: 正文容器的候选（按"越靠前越可信"排序，命中的里面再按打分挑）。
CONTENT_HINTS: tuple[tuple[str, str], ...] = (
    ("id", "js_content"),        # 微信公众号
    ("id", "mw-content-text"),   # 维基
    ("id", "content"),
    ("id", "main"),
    ("class", "post-content"),
    ("class", "article-content"),
    ("class", "markdown-body"),
    ("class", "entry-content"),
)


class Node:
    """轻量 DOM：只留转换需要的几样（标签、属性、孩子、文本）。"""

    __slots__ = ("tag", "attrs", "children", "text", "parent")

    def __init__(self, tag: str | None = None, attrs: dict[str, str] | None = None, text: str = "") -> None:
        self.tag = tag
        self.attrs = attrs or {}
        self.children: list[Node] = []
        self.text = text
        self.parent: Node | None = None


class DomBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node(tag="#root")
        self.stack = [self.root]
        self.skip = 0  # 丢掉的那几棵的深度

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag in DROP_TAGS:
            self.skip += 1
            return
        if self.skip:
            self.skip += 1 if tag not in ("br", "img", "hr", "meta", "link", "input") else 0
            return
        node = Node(tag=tag, attrs={str(k).lower(): (v or "") for k, v in attrs})
        node.parent = self.stack[-1]
        self.stack[-1].children.append(node)
        if tag not in ("br", "img", "hr", "meta", "link", "input", "source"):
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if self.skip or tag in DROP_TAGS:
            return
        node = Node(tag=tag, attrs={str(k).lower(): (v or "") for k, v in attrs})
        node.parent = self.stack[-1]
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag: str) -> None:
        if self.skip:
            self.skip -= 1
            return
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if self.skip or not data:
            return
        node = Node(text=data)
        node.parent = self.stack[-1]
        self.stack[-1].children.append(node)


def inner_text(node: Node) -> str:
    if node.tag is None:
        return node.text
    return "".join(inner_text(one) for one in node.children)


def link_text(node: Node) -> int:
    """这一棵里"包在链接里的文本"有多少字 —— 判定导航栏与正文的关键量。"""
    if node.tag == "a":
        return len(inner_text(node).strip())
    return sum(link_text(one) for one in node.children)


def block_count(node: Node) -> int:
    """这一棵里有几个像段落的块（`p`/`li`/`h*`/`pre`/`blockquote`）。"""
    if node.tag is None:
        return 0
    hit = 1 if node.tag in ("p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote") else 0
    return hit + sum(block_count(one) for one in node.children)


def score(node: Node) -> float:
    """一个节点的"这像不像正文"分数。

    三条判据都是实打实的：直接文本够长（>= 200）、**链接占比低**（超过一半就算导航）、
    段落够多（>= 3）。再按选中的容器加一点先验分（`article` / `main` 这类天生更可能是正文）。
    """
    text = inner_text(node)
    size = len(text.strip())
    if size < 200:
        return -1.0
    links = link_text(node)
    if links / max(1, size) > 0.5:
        return -1.0
    blocks = block_count(node)
    if blocks < 3:
        return -1.0
    prior = 0.0
    if node.tag in ("article", "main"):
        prior += 300.0
    for key, value in CONTENT_HINTS:
        if value and value in str(node.attrs.get(key) or ""):
            prior += 200.0
    return size + blocks * 40.0 + prior - links * 1.5


def walk(root: Node):
    """深度遍历所有**元素**节点（文本节点跳过 —— 没有标签，也就谈不上"像不像正文"）。"""
    stack = [root]
    while stack:
        node = stack.pop()
        if node.tag is not None:
            yield node
        stack.extend(node.children)


def pick_content(root: Node) -> Node | None:
    """挑正文：**站点规则优先**，都不命中才全树打分；选中之后**向上合并同级兄弟**。

    为什么规则要**优先**（实测踩过）：公众号那篇走通用打分时，选中的是包着页头的外层
    —— 正文前多出三行：重复的标题、作者栏（`原创` / `渣B渣B` / `zartbot`）、封面图。
    规则命中时不合并兄弟：`id="js_content"` 本身就是完整正文，再合并就把页头带回来了。
    """
    for node in walk(root):
        for key, value in CONTENT_HINTS:
            if value and value in str(node.attrs.get(key) or "") and score(node) > 0:
                return node

    best: Node | None = None
    best_score = -1.0
    for node in walk(root):
        one = score(node)
        if one > best_score:
            best, best_score = node, one
    if best is None or best.parent is None:
        return best
    # 正文常被切在相邻的几个 div 里：把父节点下"同样像正文"的兄弟一起收进来。
    parent = best.parent
    if parent.tag == "#root":
        return best
    siblings = [one for one in parent.children if one.tag is not None and score(one) > best_score * 0.4]
    if len(siblings) > 1:
        holder = Node(tag="#merged")
        holder.children = siblings
        return holder
    return best


def to_md(node: Node) -> str:
    """转换表本体 —— 借自 ANALYSIS 那份，逐条对应它的规则。"""
    if node.tag is None:
        return node.text
    tag = node.tag
    inner = "".join(to_md(one) for one in node.children)

    # 公众号的公式：源码存在 `data-formula` 上（`\x0a` 是它编码过的换行）
    formula = node.attrs.get("data-formula")
    if formula:
        source = formula.replace("\\x0a", "\n").strip()
        style = node.attrs.get("style", "")
        block = tag in ("section", "div", "p") or "display: block" in style or "text-align: center" in style
        return ("\n$$\n" + source + "\n$$\n") if block else ("$" + source + "$")

    if tag == "img":
        src = node.attrs.get("data-src") or node.attrs.get("src") or ""
        alt = (node.attrs.get("alt") or "").strip()
        return f"\n![{alt}]({src})\n" if src else ""
    if tag == "br":
        return "\n"
    if tag in ("strong", "b"):
        return f"**{inner.strip()}**" if inner.strip() else ""
    if tag in ("em", "i"):
        return f"*{inner.strip()}*" if inner.strip() else ""
    if tag == "a":
        href = node.attrs.get("href", "")
        text = inner.strip()
        return f"[{text}]({href})" if href and text else text
    if tag == "code":
        return f"`{inner.strip()}`" if inner.strip() else ""
    if tag == "pre":
        return "\n```\n" + inner_text(node).strip("\n") + "\n```\n"
    if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
        level = int(tag[1])
        text = inner.strip()
        return f"\n{'#' * level} {text}\n" if text else ""
    if tag == "blockquote":
        lines = [f"> {one}" for one in inner.strip().split("\n") if one.strip()]
        return "\n" + "\n".join(lines) + "\n"
    if tag == "li":
        return "- " + inner.strip() + "\n"
    if tag in ("ul", "ol"):
        items = [to_md(one) for one in node.children if one.tag == "li"]
        return "\n" + "".join(items) if items else inner
    if tag == "tr":
        cells = [inner_text(one).strip().replace("\n", " ") for one in node.children if one.tag in ("td", "th")]
        return "| " + " | ".join(cells) + " |\n" if cells else ""
    if tag == "table":
        rows = [to_md(one) for one in node.children if one.tag == "tr"]
        if not rows:
            return inner
        head, sep = rows[0], "| " + " | ".join(["---"] * max(1, rows[0].count("|") // 2)) + " |\n"
        return "\n" + "".join([head, sep] + rows[1:]) + "\n"
    if tag == "hr":
        return "\n\n---\n\n"
    if tag == "p":
        text = inner.strip()
        return f"\n\n{text}\n\n" if text else ""
    if tag in BLOCK_TAGS:
        return inner
    return inner


def tidy(md: str) -> str:
    """收尾：压空行、接回被 `<div>` 切断的句子、去掉重复的标题、收拾页头那种大段空白。

    三件都对应实测：公众号那篇的正文被十几个 `<div>` 切成一行一句；标题在页面里出现两次
    （它自己的 h1 + 我们补的 `#`）；作者栏是**一串空格排出来的对齐**（`原创` / `账号名`），
    不收拾的话，它会原样进切片。

    接法保守：只在**单换行**处接（`to_md` 把块之间写成双换行，所以单换行是同一段内的折行），
    且上一行没以句读收尾、下一行不以标点开头、两边都不是结构行、也不在代码块里。
    """
    text = re.sub(r"[ \t]+\n", "\n", md)
    text = re.sub(r"\n{3,}", "\n\n", text)
    out: list[str] = []
    fence = False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            fence = not fence
            out.append(line.rstrip())
            continue
        if not fence:
            # 页头那种靠空格对齐的东西：六个以上连续空格压成一个
            line = re.sub(r"[ \t]{6,}", " ", line)
        stripped = line.strip()
        structural = stripped.startswith(("#", "-", ">", "|", "```", "![", "$$", "$$"))
        # 重复的标题行只留第一份。比的是**最后一行非空内容**，不是紧邻的上一行 ——
        # 页面里那两行标题中间隔着一个空行（实测），比上一行的话一条都去不掉。
        last = next((one for one in reversed(out) if one.strip()), "")
        if not fence and stripped and last.strip() == stripped:
            continue
        if (
            not fence
            and stripped
            and out
            and out[-1]
            and not structural
            and not out[-1].lstrip().startswith(("#", "-", ">", "|", "```", "$$"))
            and not re.search(r"[。！？；：.!?;:”\"'）)\]】]$", out[-1])
            and not re.match(r"^[，。！？、；：)”\"'）\]】]", stripped)
        ):
            out[-1] = out[-1].rstrip() + stripped
            continue
        out.append(line.rstrip())
    return "\n".join(out).strip() + "\n"


def drop_leading_chrome(md: str) -> str:
    """丢掉正文最前面那几行"页头"：短、上下都是空行、且出现在第一段长文本之前。

    实测（公众号那篇）：正文开头是 `原创` / `渣B` / `zartbot` 三行 —— 账号栏，靠空格排的
    对齐，不收拾的话第一片切片里装的就是它。判据刻意很窄（**12 字以内 + 上下是空行 +
    此前还没出现过 40 字以上的行**），免得误伤真的短开场。
    """
    lines = md.split("\n")
    out: list[str] = []
    seen_body = False
    for index, line in enumerate(lines):
        text = line.strip()
        if len(text) >= 40:
            seen_body = True
        blank_before = index == 0 or not lines[index - 1].strip()
        blank_after = index + 1 >= len(lines) or not lines[index + 1].strip()
        byline = re.fullmatch(r"[\s*_]*\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日[^#]{0,24}", text)
        chrome = (
            not seen_body
            and text
            and not text.startswith(("#", "![", "$$"))
            and (byline or (len(text) <= 12 and blank_before and blank_after))
        )
        if chrome:
            continue
        out.append(line)
    # 丢完页头会留下成串空行，再压一遍
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out))


def title_of(raw: str, fallback: str) -> str:
    """标题：`og:title` → `<title>`；顺手去掉站点尾巴。"""
    for pattern in (
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']*)',
        r'<meta[^>]+name=["\']title["\'][^>]+content=["\']([^"\']*)',
        r"<title[^>]*>([^<]*)</title>",
    ):
        hit = re.search(pattern, raw, re.IGNORECASE)
        if hit and hit.group(1).strip():
            # 实体要还原：维基那篇存成 `<title>Flynn&#39;s taxonomy</title>`，
            # 不还原的话标题里留着 `&#39;`（实测就是这样）。
            text = html.unescape(re.sub(r"\s+", " ", hit.group(1)).strip())
            text = re.sub(r"\s*[-|·—]+\s*(微信公众平台|Wikipedia|知乎|简书|CSDN|Medium)\s*$", "", text)
            return text or fallback
    return fallback


def decode(raw: bytes) -> str:
    """按声明解出源码。保存下来的网页里常见的三种依次试。"""
    for encoding in ("utf-8", "gb18030", "big5"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def convert(path: Path) -> dict:
    """一个 HTML 文件 → `{ok, title, md, chars, blocks, why}`。"""
    raw = decode(path.read_bytes())
    builder = DomBuilder()
    builder.feed(raw)
    content = pick_content(builder.root)
    if content is None:
        return {"ok": False, "why": "没找到像正文的节点（可能整页都是脚本渲染出来的）"}
    body = drop_leading_chrome(tidy(to_md(content)))
    text_only = re.sub(r"[#>*`|\[\]()!]", "", body)
    if len(text_only.strip()) < 200:
        return {"ok": False, "why": f"正文只有 {len(text_only.strip())} 字（多半是列表页或壳页面）"}
    title = title_of(raw, path.stem)
    # 页面自己的 h1 与我们补的标题撞车时只留一份（实测：公众号那篇连着出现两次同名标题）
    first, _, rest = body.partition("\n")
    if re.sub(r"^#+\s*", "", first).strip() == title.strip():
        body = rest.lstrip("\n")
    md = f"# {title}\n\n{body}"
    return {
        "ok": True,
        "title": title,
        "md": md,
        "chars": len(text_only.strip()),
        "blocks": block_count(content),
        "images": len(re.findall(r"!\[[^\]]*\]\(([^)]+)\)", body)),
    }
