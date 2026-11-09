"""`/api/notes/*`：手机端（没有 Obsidian）读写笔记的那几个端点。

这层没有业务逻辑，全是 `vault` 的包装 —— 所以用例只验三件容易写错的事：
① 离线时确实走 `notes` 表（而不是报"没配 Obsidian"）；
② 读→写这一对的乐观锁真的接上了（另一台设备改过时不覆盖）；
③ `NoteError` 被翻成了 4xx 且 `detail` 是能直接显示的中文。
"""

from __future__ import annotations

import pytest

from app import notecloud, vault


@pytest.fixture()
def offline(monkeypatch):  # noqa: ANN001
    """这台设备没有 Obsidian：桥永远判离线，读写全走云副本。"""
    monkeypatch.setattr(vault, "_online", lambda: False)
    vault._HASH.clear()
    try:
        yield
    finally:
        vault._HASH.clear()


def test_libraries_report_the_cloud_copy(client, offline) -> None:  # noqa: ANN001
    made = client.post("/api/notes/note", json={"path": "a.md", "body": "# A"})
    assert made.status_code == 200, made.text

    got = client.get("/api/notes/libraries").json()
    assert len(got["items"]) == 1
    assert got["items"][0]["notes"] == 1
    assert got["items"][0]["name"] == made.json()["lib"]


def test_create_read_write_round_trip(client, offline) -> None:  # noqa: ANN001
    made = client.post(
        "/api/notes/note", json={"path": "日记/2026-11-09.md", "body": "# 今天\n\n第一版"}
    )
    assert made.status_code == 200, made.text
    assert made.json()["path"] == "日记/2026-11-09.md"

    got = client.get("/api/notes/note", params={"path": "日记/2026-11-09.md"}).json()
    assert got["title"] == "今天"
    assert got["body"] == "# 今天\n\n第一版"

    saved = client.put(
        "/api/notes/note",
        json={"path": "日记/2026-11-09.md", "body": "# 今天\n\n第二版"},
    ).json()
    assert saved["chars"] == len("# 今天\n\n第二版")

    again = client.get("/api/notes/note", params={"path": "日记/2026-11-09.md"}).json()
    assert again["body"] == "# 今天\n\n第二版"
    assert again["hash"] == saved["hash"]


def test_write_refuses_when_another_device_changed_the_note(
    client, db_session, offline
) -> None:  # noqa: ANN001
    client.post("/api/notes/note", json={"path": "a.md", "body": "v1"})
    client.get("/api/notes/note", params={"path": "a.md"})  # 服务端记下这次读到的 hash

    notecloud.configure(db_session)
    notecloud.write_body(notecloud.library(), "a.md", "v3")  # 模拟另一台设备推了新版本
    db_session.commit()

    res = client.put("/api/notes/note", json={"path": "a.md", "body": "v4"})
    assert res.status_code == 400
    assert "改过" in res.json()["detail"]


def test_missing_note_is_404(client, offline) -> None:  # noqa: ANN001
    res = client.get("/api/notes/note", params={"path": "没有这篇.md"})
    assert res.status_code == 404
    assert "没有这篇笔记" in res.json()["detail"]


def test_create_at_existing_path_is_400(client, offline) -> None:  # noqa: ANN001
    client.post("/api/notes/note", json={"path": "a.md", "body": ""})
    res = client.post("/api/notes/note", json={"path": "a.md", "body": ""})
    assert res.status_code == 400
    assert "已经有这一篇" in res.json()["detail"]


def test_create_without_path_or_title_is_400(client, offline) -> None:  # noqa: ANN001
    res = client.post("/api/notes/note", json={"body": "x"})
    assert res.status_code == 400
    assert "title" in res.json()["detail"]


def test_search_is_case_insensitive_and_matches_chinese(client, offline) -> None:  # noqa: ANN001
    client.post("/api/notes/note", json={"path": "积分/第一型.md", "body": "# Line Integral\n\n曲线积分"})
    client.post("/api/notes/note", json={"path": "简历.md", "body": "# 简历\n\nthe quick brown fox"})

    chinese = client.get("/api/notes/search", params={"q": "积分"}).json()
    assert chinese["count"] >= 1
    assert "积分/第一型.md" in [item["path"] for item in chinese["items"]]

    upper = client.get("/api/notes/search", params={"q": "THE"}).json()
    assert "简历.md" in [item["path"] for item in upper["items"]]


def test_tree_lists_folders_and_files(client, offline) -> None:  # noqa: ANN001
    client.post("/api/notes/note", json={"path": "积分/第一型.md", "body": "# 第一型"})
    client.post("/api/notes/note", json={"path": "顶层.md", "body": "# 顶层"})

    tree = client.get("/api/notes/tree").json()
    assert tree["dirs"]["积分"]["files"][0]["path"] == "积分/第一型.md"
    assert [item["path"] for item in tree["files"]] == ["顶层.md"]

    part = client.get("/api/notes/tree", params={"dir": "积分"}).json()
    assert part["name"] == "积分"
    assert [item["path"] for item in part["files"]] == ["积分/第一型.md"]
