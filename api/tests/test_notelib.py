"""笔记库服务的用例：目录、索引、写入、快照、改名。

这一层是"状态"（`app/notes.py` 是纯函数），所以要动真文件 —— 全部在 `tmp_path` 下跑，
把数据目录指过去，不碰仓库里的 `data/notes/`。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import notelib
from app.config import get_settings

A_BODY = "---\ntitle: A\ntags: [x]\n---\n\n# A\n\n- 一\n\t- 一·一\n- 见 [[B]]\n"


@pytest.fixture()
def lib(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> notelib.Library:
    """一个临时库：两篇笔记 + 一个附件。"""
    monkeypatch.setenv("QF_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    notelib.reset_index()

    root = tmp_path / "notes" / "T"
    (root / "asset").mkdir(parents=True)
    (root / "A.md").write_text(A_BODY, encoding="utf-8")
    (root / "B.md").write_text("---\ntitle: B\n---\n\nB 正文，附图 ![[图.png]]\n", encoding="utf-8")
    (root / "asset" / "图.png").write_bytes(b"\x89PNG\r\n")
    yield notelib.library("T")

    get_settings.cache_clear()
    notelib.reset_index()


# ------------------------------------------------------------------ 读


def test_tree_and_stats(lib: notelib.Library):
    tree = notelib.tree(lib)
    assert tree["count"] == 2
    assert sorted(item["name"] for item in tree["files"]) == ["A.md", "B.md"]
    stats = notelib.stats()
    assert stats["notes"] == 2
    assert stats["links"] == 2  # A → B，B → 图.png


def test_read_note_gives_outline_backlinks_and_indent_unit(lib: notelib.Library):
    note = notelib.read_note(lib, "A.md")
    assert note["title"] == "A"
    assert note["tags"] == ["x"]
    assert note["indent_unit"] == "\t"
    assert [item["level"] for item in note["outline"]] == [0, 1, 1, 2, 1]
    assert [item["target"] for item in note["outlinks"]] == ["B"]
    assert note["backlinks"] == []

    b = notelib.read_note(lib, "B.md")
    assert b["backlinks"][0]["path"] == "A.md"


def test_links_to_attachments_are_not_unresolved(lib: notelib.Library):
    """**回归**：指向附件的引用曾经被报成"断链"（32 条里 25 条是这种误报）。

    附件不是笔记，但它确实存在 —— 它不是断链。只有谁都找不到的才算。
    """
    note = notelib.read_note(lib, "B.md")
    assert note["unresolved"] == []
    assert note["outlinks"][0]["kind"] == "asset"


def test_links_to_nothing_are_still_reported(lib: notelib.Library):
    path = lib.root / "C.md"
    path.write_text("指 [[根本不存在]] 与 ![[没有这张图.png]]\n", encoding="utf-8")
    notelib.reset_index()
    note = notelib.read_note(lib, "C.md")
    assert sorted(note["unresolved"]) == ["根本不存在", "没有这张图.png"]  # 按出现顺序：先笔记、后附件


def test_search_prefers_title_hits_and_finds_body(lib: notelib.Library):
    assert [hit["path"] for hit in notelib.search("A", lib_name="T")][0] == "A.md"
    assert [hit["path"] for hit in notelib.search("附图", lib_name="T")] == ["B.md"]


def test_tag_list_merges_header_and_inline_tags(lib: notelib.Library):
    (lib.root / "D.md").write_text("---\ntags: [数学]\n---\n\n正文 #微积分\n", encoding="utf-8")
    notelib.reset_index()
    rows = {row["tag"]: row["count"] for row in notelib.tag_list("T")}
    assert rows == {"数学": 1, "微积分": 1, "x": 1}


# ------------------------------------------------------------------ 写


def test_line_op_insert_and_indent_round_trip_to_disk(lib: notelib.Library):
    note = notelib.line_op(lib, "A.md", op="insert", index=4, raw="- 二")
    assert note["body"].endswith("- 二\n")
    note = notelib.line_op(lib, "A.md", op="indent", index=5)
    assert note["body"].splitlines()[5] == "\t- 二"
    # 落盘了才算数：磁盘上的**正文末行**也是缩进过的（行号要算上元数据头那几行）
    assert (lib.root / "A.md").read_text(encoding="utf-8").splitlines()[-1] == "\t- 二"


def test_move_swaps_with_the_neighbour_block(lib: notelib.Library):
    """Alt+↓ / Alt+↑：把一块连同子树与后面的块换位。"""
    before = notelib.read_note(lib, "A.md")["body"].splitlines()
    note = notelib.line_op(lib, "A.md", op="move", index=2, delta=1)   # "- 一" 往下
    after = note["body"].splitlines()
    assert after[2] == "- 见 [[B]]"
    assert after[3] == "- 一"
    assert after[4] == "\t- 一·一"      # 子树跟着走
    notelib.line_op(lib, "A.md", op="move", index=3, delta=-1)
    assert notelib.read_note(lib, "A.md")["body"].splitlines() == before


def test_undo_walks_back_through_several_changes(lib: notelib.Library):
    """**回归**：连着撤销要能一直往回退。

    原先每次撤销都把当前状态也存一份，于是"最新那份"永远是刚撤掉的那版 ——
    再点一次就在最后两个状态之间来回切，怎么点都退不回去（实测连撤 11 次都没干净）。
    """
    start = notelib.read_note(lib, "A.md")["body"]
    for step in range(4):
        notelib.line_op(lib, "A.md", op="replace", index=0, raw=f"# 第 {step} 版")
    assert "第 3 版" in notelib.read_note(lib, "A.md")["body"]

    for _ in range(4):
        notelib.undo(lib, "A.md")
    assert notelib.read_note(lib, "A.md")["body"] == start
    with pytest.raises(notelib.NoteError):
        notelib.undo(lib, "A.md")   # 已经是最早那一版，如实报错而不是空转


def test_restore_picks_a_specific_version(lib: notelib.Library):
    notelib.line_op(lib, "A.md", op="replace", index=0, raw="# 第一版")
    notelib.line_op(lib, "A.md", op="replace", index=0, raw="# 第二版")
    versions = notelib.snapshots(lib, "A.md")
    assert len(versions) >= 2
    # 最早那一版就是原始内容
    notelib.restore(lib, "A.md", versions[-1]["name"])
    assert notelib.read_note(lib, "A.md")["body"].startswith("# A")
    with pytest.raises(notelib.NoteNotFound):
        notelib.restore(lib, "A.md", "../不存在.snap")


def test_write_body_keeps_the_header(lib: notelib.Library):
    notelib.write_body(lib, "A.md", "换掉的正文\n", why="test")
    text = (lib.root / "A.md").read_text(encoding="utf-8")
    assert text == "---\ntitle: A\ntags: [x]\n---\n\n换掉的正文\n"


def test_set_meta_only_changes_the_given_keys(lib: notelib.Library):
    note = notelib.set_meta(lib, "A.md", {"title": "A 改"})
    assert note["meta"] == {"title": "A 改", "tags": ["x"]}
    assert "一·一" in note["body"]  # 正文一字未动


def test_create_note_then_rename_updates_incoming_links(lib: notelib.Library):
    notelib.create_note(lib, "", "刚建的")
    assert (lib.root / "刚建的.md").is_file()
    with pytest.raises(notelib.NoteError):
        notelib.create_note(lib, "", "刚建的")  # 重名要报错，不是覆盖

    result = notelib.rename_note(lib, "B.md", "B 新名.md")
    assert result["updated_notes"] == ["A.md"]
    # 链接的写法照原样保留：原来只写文件名，改完还只写文件名
    assert "[[B 新名]]" in notelib.read_note(lib, "A.md")["body"]
    assert not (lib.root / "B.md").exists()
    assert notelib.read_note(lib, "B 新名.md")["backlinks"][0]["path"] == "A.md"


def test_path_traversal_is_refused(lib: notelib.Library):
    with pytest.raises(notelib.NoteError):
        notelib.read_note(lib, "../../etc/passwd")
    with pytest.raises(notelib.NoteError):
        notelib.safe_path(lib, "asset/../../../escape.md")


def test_missing_note_raises_not_found(lib: notelib.Library):
    with pytest.raises(notelib.NoteNotFound):
        notelib.read_note(lib, "没有这篇.md")
    with pytest.raises(notelib.NoteNotFound):
        notelib.library("不存在的库")


def test_broken_header_is_refused_instead_of_being_rewritten(lib: notelib.Library):
    """头没闭合的笔记**不许编辑** —— 补一个头会变成两个头叠着，比原样留着更糟。"""
    (lib.root / "坏头.md").write_text("---\ntitle: 忘了闭合\n\n正文\n", encoding="utf-8")
    notelib.reset_index()
    with pytest.raises(notelib.NoteError, match="没有闭合"):
        notelib.line_op(lib, "坏头.md", op="insert", index=0, raw="新行")


def test_canvas_is_a_first_class_entry_and_counts_its_cards(lib: notelib.Library):
    (lib.root / "图.canvas").write_text(
        '{"nodes": [{"id": "n1", "type": "file", "file": "A.md"}], "edges": []}',
        encoding="utf-8",
    )
    notelib.reset_index()
    note = notelib.read_note(lib, "图.canvas")
    assert note["kind"] == "canvas"
    assert note["linked"] == ["A.md"]
    # 画布指向某篇笔记 → 那篇笔记的反链里应当出现画布（最容易被漏掉的一种引用）
    assert any(item["path"] == "图.canvas" for item in notelib.read_note(lib, "A.md")["backlinks"])
