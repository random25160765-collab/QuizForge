"""笔记解析的规则用例。

为什么要单独测这个：**解析规则是导入器与后端共用的那一份**（`app/notes.py`）。
它一旦悄悄变了，"导入时算出来的反链"和"界面上看到的反链"就会不一致，
而且是不一致到用户发现为止。这里把几条容易走偏的规则钉住。
"""

from __future__ import annotations

from pathlib import Path

from app.notes import (
    GENERATED_KEY,
    build_index,
    iter_links,
    minimal_meta,
    note_id,
    render,
    resolve,
    sha256_bytes,
    sha256_file,
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
