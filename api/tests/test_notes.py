"""笔记解析的规则用例。

为什么要单独测这个：**解析规则是导入器与后端共用的那一份**（`app/notes.py`）。
它一旦悄悄变了，"导入时算出来的反链"和"界面上看到的反链"就会不一致，
而且是不一致到用户发现为止。这里把几条容易走偏的规则钉住。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.notes import (
    GENERATED_KEY,
    block_extent,
    build_index,
    delete_line,
    detect_indent_unit,
    head_block,
    insert_line,
    iter_inline_tags,
    iter_links,
    minimal_meta,
    move_block,
    note_id,
    outline,
    render,
    replace_line,
    resolve,
    rewrite_links,
    sha256_bytes,
    sha256_file,
    shift_line,
    splice_body,
    split_text,
)


# ------------------------------------------------------------------ 元数据头


def test_note_without_header_keeps_body_verbatim():
    text = "# 标题\n\n正文\n"
    note = split_text(text)
    assert note.had_header is False
    assert note.broken_header is False
    assert note.body == text
    assert note.meta == {}


def test_note_with_header_splits_meta_and_body():
    note = split_text("---\ntitle: 卷积\ntags: [信号]\n---\n\n正文第一行\n")
    assert note.had_header is True
    assert note.meta == {"title": "卷积", "tags": ["信号"]}
    assert note.body == "正文第一行\n"


def test_header_that_is_never_closed_is_reported_not_guessed():
    """没闭合的 `---` **不当头**、也不许补头。

    补一个头会变成两个头叠在一起 —— 比原样留着更糟。所以如实标 broken，
    让导入清单里出现这一条：那是源文件的问题，不该我猜。
    """
    text = "---\ntitle: 忘了闭合\n\n正文\n"
    note = split_text(text)
    assert note.broken_header is True
    assert note.had_header is False
    assert note.body == text


def test_horizontal_rule_is_not_a_header():
    """第 1 行就是正文时，后面出现的 `---` 只是正文里的一条分隔线。"""
    text = "# 线性空间\n---\n正文\n"
    note = split_text(text)
    assert note.had_header is False
    assert note.body == text


def test_minimal_meta_takes_title_from_first_heading_then_stem():
    with_heading = minimal_meta("# 极限的运算\n\n正文\n", "未命名")
    assert with_heading["title"] == "极限的运算"
    assert with_heading["tags"] == []
    # 标记这一行不是作者写的 —— 让"哪一行是导入器补的"永远可识别、可批量回退
    assert with_heading[GENERATED_KEY] is True

    no_heading = minimal_meta("正文没有标题\n", "极限的运算")
    assert no_heading["title"] == "极限的运算"
    assert no_heading["tags"] == []


def test_render_round_trips_through_split():
    meta = minimal_meta("# 标题\n\n正文\n", "文件")
    text = render(meta, "# 标题\n\n正文\n")
    again = split_text(text)
    assert again.had_header is True
    assert again.generated is True
    assert again.meta["title"] == "标题"
    assert again.body == "# 标题\n\n正文\n"


# ------------------------------------------------------------------ 引用


def test_links_keep_alias_anchor_and_embed_flag():
    text = "![[1.4 极限的运算.pdf#page=19&rect=1,2,3,4|1.4 极限的运算, p.19]]\n[[夹逼准则]]\n"
    links = list(iter_links(text))
    assert len(links) == 2
    first, second = links
    assert first.target == "1.4 极限的运算.pdf"
    assert first.anchor == "page=19&rect=1,2,3,4"
    assert first.alias == "1.4 极限的运算, p.19"
    assert first.embed is True
    assert first.is_file is True
    assert second.target == "夹逼准则"
    assert second.is_file is False


def test_links_inside_code_are_not_links():
    """代码里写着 `[[x]]` 不该变成一条反链 —— 代码块要当哑内容看。"""
    text = (
        "正文 [[真的链接]]\n\n"
        "```python\n"
        "pattern = r'[[假的链接]]'\n"
        "```\n\n"
        "行内代码 `[[也是假的]]`\n"
    )
    targets = [link.target for link in iter_links(text)]
    assert targets == ["真的链接"]


def test_markdown_links_are_parsed_but_external_ones_are_not():
    text = "[本地的](asset/图.png)\n[外部的](https://example.com/a.png)\n"
    links = list(iter_links(text))
    assert [link.target for link in links] == ["asset/图.png"]
    assert links[0].is_file is True


# ------------------------------------------------------------------ 解析目标


def test_resolve_by_path_then_by_name():
    rels = {"asset/图.png", "data/1 高等数学/数列的极限.md"}
    by_name = build_index(rels)

    # 按完整相对路径
    assert resolve("asset/图.png", by_name=by_name, rels=rels).rel == "asset/图.png"
    # 双链通常只写文件名（文件在深层目录里）→ 按文件名找
    assert resolve("数列的极限", by_name=by_name, rels=rels).rel == "data/1 高等数学/数列的极限.md"
    # 相对当前笔记所在目录（`./` 前缀也要能认）
    found = resolve("./图.png", by_name=by_name, rels=rels, current_dir="asset")
    assert found.rel == "asset/图.png"
    # 找不到就是找不到，不猜
    assert not resolve("不存在的笔记", by_name=by_name, rels=rels)


def test_resolve_reports_ambiguity_instead_of_pretending():
    rels = {"a/同名.md", "b/同名.md"}
    found = resolve("同名", by_name=build_index(rels), rels=rels)
    assert found.rel == "a/同名.md"      # 取排序第一个
    assert found.ambiguous is True       # 但要说出来


def test_note_id_is_relative_to_the_library():
    """引用键不能带机器相关的绝对路径 —— 换台机器就没了。"""
    assert note_id("Math", "data/1 高等数学/数列的极限.md") == "Math/data/1 高等数学/数列的极限.md"
    assert ":" not in note_id("Math", "a.md")


# ------------------------------------------------------------------ 指纹


def test_sha256_helpers_agree(tmp_path: Path):
    path = tmp_path / "note.md"
    path.write_bytes("内容\n".encode())
    assert sha256_file(path) == sha256_bytes("内容\n".encode())


# ------------------------------------------------------------------ 大纲（幕布那半边）


def test_outline_nests_bullets_under_headings():
    body = "# 第一节\n\n- 一\n\t- 一·一\n- 二\n"
    lines = outline(body)
    assert [(item.level, item.kind) for item in lines] == [
        (0, "heading"),
        (1, "blank"),
        (1, "bullet"),
        (2, "bullet"),
        (1, "bullet"),
    ]
    # 标题下面的内容归它一层；第一节那个条目可以折叠
    assert lines[2].foldable is True
    assert lines[4].foldable is False


def test_outline_counts_indented_plain_text_as_nesting():
    """**回归**：普通文字靠缩进表达层级，也必须算。

    实测那个库的 MENU 笔记就是这种写法（"前置知识" 下面用制表符顶出一组条目，
    没有项目符号）。只认项目符号的话，整篇 MENU 会摊平成一层 —— 幕布那半边直接废掉。
    """
    body = "前置知识\n\t抽象代数基本概念\n\t多项式\n线性空间\n\t坐标化\n"
    assert [(item.level, item.text) for item in outline(body)] == [
        (0, "前置知识"),
        (1, "抽象代数基本概念"),
        (1, "多项式"),
        (0, "线性空间"),
        (1, "坐标化"),
    ]


def test_outline_stays_relative_so_two_space_and_tab_both_work():
    two_space = "- 一\n  - 一·一\n- 二\n"
    tabbed = "- 一\n\t- 一·一\n- 二\n"
    assert [item.level for item in outline(two_space)] == [0, 1, 0]
    assert [item.level for item in outline(tabbed)] == [0, 1, 0]


def test_outline_marks_code_and_lets_blank_follow_the_next_line():
    body = "前\n\n```python\n[[这不是引用]]\n```\n后\n"
    lines = outline(body)
    kinds = [item.kind for item in lines]
    assert kinds == ["text", "blank", "code", "code", "code", "text"]
    assert lines[1].level == lines[0].level  # 空行跟着上下文，不在折叠处断开


def test_detect_indent_unit_follows_the_file():
    assert detect_indent_unit("- 一\n\t- 二\n") == "\t"
    assert detect_indent_unit("- 一\n  - 二\n") == "  "


# ------------------------------------------------------------------ 行级操作


def test_line_operations_keep_the_trailing_newline():
    body = "- 一\n- 二\n"
    assert insert_line(body, 1, "- 一点五") == "- 一\n- 二\n- 一点五\n"
    assert replace_line(body, 0, "- 改") == "- 改\n- 二\n"
    assert delete_line(body, 0) == "- 二\n"
    # 结尾没有换行的文件不该被加上一个
    assert insert_line("- 一", 0, "- 二") == "- 一\n- 二"


def test_shift_line_indents_and_outdents_without_eating_content():
    body = "- 一\n"
    indented = shift_line(body, 0, 1, "\t")
    assert indented == "\t- 一\n"
    assert shift_line(indented, 0, -1, "\t") == "- 一\n"
    # 顶格再退还是顶格（不会把 `-` 吃掉）
    assert shift_line(body, 0, -1, "\t") == "- 一\n"


def test_move_block_takes_the_whole_subtree():
    body = "- 一\n\t- 一·一\n\t- 一·二\n- 二\n"
    # 子树的边界：第 0 行的块 = 第 0~2 行（含两条更深的子项）
    assert block_extent(body, 0) == (0, 3)
    # `to` 按**移动前**的行号：0 与 3 都是原地不动，4 是挪到 "- 二" 之后
    assert move_block(body, 0, 0) == body
    assert move_block(body, 0, 3) == body
    moved = move_block(body, 0, 4)
    assert moved.split("\n")[:4] == ["- 二", "- 一", "\t- 一·一", "\t- 一·二"]
    # 往回挪也一样（把整块挪到最前；注意 index 要指向**块的起始行**）
    assert move_block(moved, 1, 0).split("\n")[:4] == [
        "- 一",
        "\t- 一·一",
        "\t- 一·二",
        "- 二",
    ]
    # 挪进自己内部要报错，而不是悄悄弄坏文件
    with pytest.raises(ValueError):
        move_block(body, 0, 1)


# ------------------------------------------------------------------ 改名


def test_rewrite_links_keeps_alias_and_anchor():
    text = "见 [[旧名#小节|别名]] 与 ![[asset/旧名.png#page=3]]\n"
    new, count = rewrite_links(
        text, lambda target: "新名" if target == "旧名" else None
    )
    assert count == 1
    assert new == "见 [[新名#小节|别名]] 与 ![[asset/旧名.png#page=3]]\n"


def test_rewrite_links_skips_code_and_inline_code():
    """改名最怕的就是把"文档里举的例子"也改了。"""
    text = "正文 [[旧名]]\n\n```\n[[旧名]]\n```\n\n行内 `[[旧名]]`\n"
    new, count = rewrite_links(text, lambda target: "新名" if target == "旧名" else None)
    assert count == 1
    assert "```\n[[旧名]]\n```" in new
    assert "`[[旧名]]`" in new
    assert "正文 [[新名]]" in new


def test_rewrite_links_handles_markdown_links():
    text = "[说明](目录/旧名.md)\n"
    new, count = rewrite_links(text, lambda target: "新目录/新名.md" if target.endswith("旧名.md") else None)
    assert (new, count) == ("[说明](新目录/新名.md)\n", 1)


# ------------------------------------------------------------------ 行内标签


def test_inline_tags_do_not_fire_on_latex_or_sentences():
    """**回归**：宽松的正则会把 LaTeX 与整句中文都当成标签。

    实测在一篇真实数学笔记里，它把 `#\\{i|\\dim` 与 "井号代表集合内元素的个数%%"
    都算成了标签 —— 于是标签面板里全是垃圾。
    """
    body = (
        "集合 $A$ 的势记作 $#\\{i|\\dim\\}$\n"
        "井号代表集合内元素的个数%%\n"
        "# 这是标题不是标签\n"
        "真的标签 #微积分 与 #linear-algebra\n"
    )
    assert list(iter_inline_tags(body)) == ["微积分", "linear-algebra"]


# ------------------------------------------------------------------ 正文拼接


def test_splice_body_keeps_the_original_header_byte_for_byte():
    text = "---\ntitle: A\ntags: [x]   # 作者自己写的风格\n---\n\n旧正文\n"
    new = splice_body(text, "新正文\n")
    assert new.startswith("---\ntitle: A\ntags: [x]   # 作者自己写的风格\n---\n\n新正文\n")
    assert head_block(text) == "---\ntitle: A\ntags: [x]   # 作者自己写的风格\n---\n"
