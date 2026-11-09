"""`vault` 的路由：桥在走信箱，桥不在走云副本。

为什么单独测这一层：`tools.py` 的五个笔记工具**不感知**后端是谁，全靠这里
分岔。分岔一旦写错（比如桥在线时也碰了表），桌面端会拿云副本覆盖本地文件 ——
那是数据事故，不是样式问题。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import notecloud, vault
from app.models import Note
from app.notelib import Library


@pytest.fixture()
def cloud(db_session):  # noqa: ANN001
    """空表 + 两个模块都注入会话。"""
    vault.configure(db_session)
    vault._HASH.clear()
    db_session.query(Note).delete()
    db_session.commit()
    try:
        yield db_session
    finally:
        db_session.query(Note).delete()
        db_session.commit()
        vault._HASH.clear()
        notecloud.configure(None)


@pytest.fixture()
def lib() -> Library:
    return Library(name="vault", root=Path("."))


def test_read_falls_back_to_cloud_when_bridge_is_offline(cloud, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(vault, "_online", lambda: False)

    notecloud.configure(cloud)
    notecloud.create_at(Library(name="vault", root=Path(".")), "a.md", "# A\n正文")

    got = vault.read_note(Library(name="vault", root=Path(".")), "a.md")
    assert got["body"] == "# A\n正文"
    assert got["hash"] == notecloud.sha16("# A\n正文")
    # 读完之后 hash 要进缓存，下一步 write 才有乐观锁可用
    assert vault._HASH[("vault", "a.md")] == got["hash"]


def test_bridge_path_never_touches_the_table(cloud, monkeypatch) -> None:  # noqa: ANN001
    calls: list[str] = []

    def fake_call(op, params=None, *, expect="5s"):  # noqa: ANN001
        calls.append(op)
        return {"lib": "vault", "path": "a.md", "body": "桥给的", "hash": "h1"}

    monkeypatch.setattr(vault, "_online", lambda: True)
    monkeypatch.setattr(vault, "_call", fake_call)

    got = vault.read_note(Library(name="vault", root=Path(".")), "a.md")
    assert got["body"] == "桥给的"
    assert calls == ["read"]
    assert cloud.query(Note).count() == 0  # 表里一行都没动


def test_write_falls_back_and_carries_base_hash(cloud, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(vault, "_online", lambda: False)
    cloud_lib = Library(name="vault", root=Path("."))
    notecloud.create_at(cloud_lib, "a.md", "v1")

    vault.read_note(cloud_lib, "a.md")  # 拿 hash
    vault.write_body(cloud_lib, "a.md", "v2")
    assert notecloud.read_note(cloud_lib, "a.md")["body"] == "v2"

    # 模拟另一台设备改过：缓存里的旧 hash 就不再匹配
    notecloud.write_body(cloud_lib, "a.md", "v3")
    with pytest.raises(vault.NoteError) as exc:
        vault.write_body(cloud_lib, "a.md", "v4")
    assert "改过" in str(exc.value)


def test_libraries_falls_back_to_table_count(cloud, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(vault, "_online", lambda: False)
    notecloud.create_at(Library(name="vault", root=Path(".")), "a.md", "x")

    libs = vault.libraries()
    assert len(libs) == 1
    assert libs[0].notes == 1


def test_open_note_refuses_without_obsidian(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(vault, "_online", lambda: False)
    with pytest.raises(vault.NoteError) as exc:
        vault.open_note("a.md")
    assert "没有 Obsidian" in str(exc.value)


def test_ping_never_falls_back(monkeypatch) -> None:  # noqa: ANN001
    """「检测连接」必须如实说本地 Obsidian 在不在 —— 不能拿云副本冒充。"""
    monkeypatch.setattr(vault, "_vault_path", lambda: "")
    with pytest.raises(vault.NoteError):
        vault.ping()
