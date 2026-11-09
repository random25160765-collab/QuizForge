"""云副本（`notes` 表）的规则用例。

断言分两类，都是**契约**而不是实现细节：

1. 与 `obsidian-bridge/main.js` 同口径的算法（`sha16` / frontmatter 切分 /
   大纲行号）。两边一分叉，冲突检测会永远误报 —— 那种"偶尔发生"的误报最难查。
2. 降级层的语义：乐观锁、路径越界、找不到时的错误类型。这些是 LLM 工具
   直接依赖的行为（它 `except NoteNotFound` 与 `except NoteError` 分得开）。
"""

from __future__ import annotations

import pytest

from app import notecloud
from app.models import Note
from app.notelib import NoteError, NoteNotFound


@pytest.fixture()
def cloud(db_session):  # noqa: ANN001
    """空表 + 已注入会话。`notecloud` 写操作会 commit，所以用例之间要显式清。"""
    notecloud.configure(db_session)
    db_session.query(Note).delete()
    db_session.commit()
    try:
        yield db_session
    finally:
        db_session.query(Note).delete()
        db_session.commit()
        notecloud.configure(None)


@pytest.fixture()
def lib(cloud):  # noqa: ANN001
    return notecloud.libraries()[0]


# ------------------------------------------------------------------ 与插件同口径


def test_sha16_matches_plugin_sha256_prefix() -> None:
    # Node: crypto.createHash('sha256').update('abc','utf8').digest('hex').slice(0,16)
    assert notecloud.sha16("abc") == "ba7816bf8f01cfea"
    assert notecloud.sha16("") == "e3b0c44298fc1c14"


def test_split_front_uses_plugin_regex_not_yaml() -> None:
    head, body = notecloud.split_front("---\ntitle: 日记\n---\n\n正文")
    assert head == "---\ntitle: 日记\n---\n"
    assert body == "\n正文"

    # 没有头：整篇都是正文
    assert notecloud.split_front("正文") == ("", "正文")

    # **坏头**（没有闭合）：插件也是原样当正文，不做 YAML 修复
    broken = "---\ntitle: 日记\n正文"
    assert notecloud.split_front(broken) == ("", broken)


def test_outline_line_numbers_start_at_one() -> None:
    rows = notecloud.outline_of("前言\n# 一\n正文\n## 1.1\n")
    assert rows == [
        {"level": 1, "text": "一", "line": 2},
        {"level": 2, "text": "1.1", "line": 4},
    ]


# ------------------------------------------------------------------ 读 / 写


def test_create_read_roundtrip(cloud, lib) -> None:  # noqa: ANN001
    notecloud.create_at(lib, "日记/2026-11-09.md", "# 今天\n\n写了点东西。")

    got = notecloud.read_note(lib, "日记/2026-11-09.md")
    assert got["path"] == "日记/2026-11-09.md"
    assert got["title"] == "今天"
    assert got["body"] == "# 今天\n\n写了点东西。"
    assert got["chars"] == len(got["body"])
    assert got["hash"] == notecloud.sha16(got["body"])
    assert got["kind"] == "note"
    assert got["backlinks"] == []
    assert got["outline"][0] == {"level": 1, "text": "今天", "line": 1}


def test_title_falls_back_to_stem(cloud, lib) -> None:  # noqa: ANN001
    notecloud.create_at(lib, "无标题.md", "只有正文")
    assert notecloud.read_note(lib, "无标题.md")["title"] == "无标题"


def test_write_body_updates_hash_and_chars(cloud, lib) -> None:  # noqa: ANN001
    notecloud.create_at(lib, "a.md", "旧")
    result = notecloud.write_body(lib, "a.md", "新的正文")
    assert result["hash"] == notecloud.sha16("新的正文")
    assert result["chars"] == 4
    assert notecloud.read_note(lib, "a.md")["body"] == "新的正文"


def test_write_body_optimistic_lock(cloud, lib) -> None:  # noqa: ANN001
    notecloud.create_at(lib, "a.md", "v1")
    old = notecloud.read_note(lib, "a.md")["hash"]

    # 另一台设备经同步改了这篇
    notecloud.write_body(lib, "a.md", "v2")

    with pytest.raises(NoteError) as exc:
        notecloud.write_body(lib, "a.md", "v3", base_hash=old)
    assert "改过" in str(exc.value)
    assert notecloud.read_note(lib, "a.md")["body"] == "v2"


def test_missing_note_raises_not_found(cloud, lib) -> None:  # noqa: ANN001
    with pytest.raises(NoteNotFound):
        notecloud.read_note(lib, "不存在.md")
    with pytest.raises(NoteNotFound):
        notecloud.write_body(lib, "不存在.md", "正文")


def test_create_refuses_duplicate(cloud, lib) -> None:  # noqa: ANN001
    notecloud.create_at(lib, "a.md", "x")
    with pytest.raises(NoteError) as exc:
        notecloud.create_at(lib, "a.md", "y")
    assert "已经有这一篇" in str(exc.value)


def test_create_note_builds_path_from_folder_and_title(cloud, lib) -> None:  # noqa: ANN001
    got = notecloud.create_note(lib, "技术/笔记", "云同步")
    assert got["path"] == "技术/笔记/云同步.md"


# ------------------------------------------------------------------ 路径


@pytest.mark.parametrize(
    "bad",
    ["", "/", "../etc/passwd", "a/../../b.md", ".quizforge-bridge/req.json", "a" * 600 + ".md"],
)
def test_path_guards(cloud, lib, bad: str) -> None:  # noqa: ANN001
    with pytest.raises(NoteError):
        notecloud.read_note(lib, bad)


def test_windows_separators_are_normalized(cloud, lib) -> None:  # noqa: ANN001
    notecloud.create_at(lib, "日记\\2026-11-09.md", "x")
    assert notecloud.read_note(lib, "日记/2026-11-09.md")["path"] == "日记/2026-11-09.md"


# ------------------------------------------------------------------ 找 / 列


def test_search_matches_path_and_body(cloud, lib) -> None:  # noqa: ANN001
    notecloud.create_at(lib, "数学/极限.md", "夹逼定理")
    notecloud.create_at(lib, "技术/同步.md", "Turso 与 Hrana")
    notecloud.create_at(lib, "杂项.md", "夹逼没什么关系")

    hits = notecloud.search("夹逼")
    assert {item["path"] for item in hits} == {"数学/极限.md", "杂项.md"}

    # 路径也参与匹配
    assert [item["path"] for item in notecloud.search("技术")] == ["技术/同步.md"]


def test_search_strips_tag_and_title_filters(cloud, lib) -> None:  # noqa: ANN001
    notecloud.create_at(lib, "读书.md", "# 读书笔记\n\n夹逼")
    notecloud.create_at(lib, "别.md", "夹逼")

    # `#tag` 没有 frontmatter 可查，直接丢掉；不能拿 "#tag" 去正文里找
    assert {item["path"] for item in notecloud.search("夹逼 #数学")} == {"读书.md", "别.md"}

    # `title:` 走标题（正文首行 H1 / 文件名）
    hits = notecloud.search("title:读书 夹逼")
    assert [item["path"] for item in hits] == ["读书.md"]


def test_tree_is_nested_with_files(cloud, lib) -> None:  # noqa: ANN001
    notecloud.create_at(lib, "a.md", "A")
    notecloud.create_at(lib, "日记/b.md", "B")
    notecloud.create_at(lib, "日记/2026/c.md", "C")

    tree = notecloud.tree(lib)
    assert [item["path"] for item in tree["files"]] == ["a.md"]
    assert set(tree["dirs"]) == {"日记"}
    diary = tree["dirs"]["日记"]
    assert [item["path"] for item in diary["files"]] == ["日记/b.md"]
    assert [item["path"] for item in diary["dirs"]["2026"]["files"]] == ["日记/2026/c.md"]

    # 指定目录：只列那一段，根换成目录名
    sub = notecloud.tree(lib, "日记")
    assert sub["name"] == "日记"
    assert [item["path"] for item in sub["files"]] == ["日记/b.md"]


def test_libraries_reports_note_count(cloud, lib) -> None:  # noqa: ANN001
    notecloud.create_at(lib, "a.md", "x")
    notecloud.create_at(lib, "b.md", "y")
    assert notecloud.libraries()[0].notes == 2
