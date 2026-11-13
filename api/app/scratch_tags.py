"""碎片里的**标签**：从正文里解析出来，不存第二份。

## 为什么不加一列 `tags`

标签就写在正文里（`#Books/札记/grammar 这句话……`），这是 flomo 的做法，
也是碎片这种"写一句就走"的东西该有的做法 —— 让人先选分类再写字，那它就成了笔记。

只要正文是唯一事实来源，标签就不可能和正文对不上：改字就是改标签，删字就是删标签。
代价是每次读都要解析一遍（这页要标签树、要按标签筛），但碎片是短句，
一份几千条的碎片解析一次是毫秒级 —— 比维护一张会漂的第二份表便宜得多。

（与 `docs/` 里那条一贯的规矩一致：关系靠解析，不靠标注。）

## 路径

`/` 分层，`Books/札记/《CollegeWriting》/grammar` 同时算四条标签：
`Books`、`Books/札记`、`Books/札记/《CollegeWriting》`、`…/grammar`。
父节点的条数是**包含子节点**的（与 flomo 那个 9 一致），否则树的中间层全是 0。
"""

from __future__ import annotations

import re

#: `#` 后面的一串：到空白或中文/英文标点为止。
#: `《》`、`-`、`_` 都算标签的一部分（用户的书名标签就带书名号）；
#: 括号与引号不算 —— `（见 #语法）` 这种写法里，标签不该把右括号吃进去。
_TAG_RE = re.compile(r"#([^\s#，。！？；：、,.!?;:()（）\[\]【】\"“”'']+)")

#: 一条标签最长多少字。防的是"整段话被当成一个标签"（没有标点的一长串）。
_MAX_TAG = 120


def normalize(raw: str) -> str:
    """`#a//b/` → `a/b`：去掉空层、首尾空白，并限长。"""
    parts = [one.strip() for one in str(raw or "").split("/")]
    return "/".join(one for one in parts if one)[:_MAX_TAG]


def parse(body: str) -> list[str]:
    """正文里出现的标签（按出现顺序、去重、保留完整路径）。"""
    found: list[str] = []
    for match in _TAG_RE.finditer(str(body or "")):
        path = normalize(match.group(1))
        if path and path not in found:
            found.append(path)
    return found


def under(tag: str, path: str) -> bool:
    """`path` 是不是 `tag` 这一支（含自己）—— 点父标签要看得见子标签的碎片。"""
    tag = normalize(tag)
    if not tag:
        return True
    return path == tag or path.startswith(tag + "/")


def count_tree(bodies) -> list[dict]:  # noqa: ANN001
    """把"每条带哪些标签"折成一棵带条数的树。

    返回的是嵌套结构（`children`），前端照着递归画就是缩进；
    条数算在**每一个祖先**上（父节点 = 含子节点的条数）。
    """
    counts: dict[str, int] = {}
    for body in bodies:
        for path in parse(body):
            parts = path.split("/")
            for depth in range(1, len(parts) + 1):
                key = "/".join(parts[:depth])
                counts[key] = counts.get(key, 0) + 1

    roots: list[dict] = []
    index: dict[str, dict] = {}
    for path in sorted(counts):  # 字典序：同一层里名字相近的挨在一起，比"按条数"稳定
        parts = path.split("/")
        node = {
            "path": path,
            "name": parts[-1],
            "count": counts[path],
            "children": [],
        }
        index[path] = node
        parent = "/".join(parts[:-1])
        if parent and parent in index:
            index[parent]["children"].append(node)
        else:
            roots.append(node)
    return roots
