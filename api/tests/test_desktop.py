"""在系统文件管理器里打开目录 —— 只测「拼什么命令」与「守住入口」，不真去开窗口。

这是**唯一**一处我们会去启动别的程序，所以两条边界都要钉住：
命令按平台拼对（不猜 PATH、不经 shell），以及"库路径由服务端取"那条守卫。
"""

from __future__ import annotations

from app import desktop


def test_command_per_platform() -> None:
    """三个平台各一条命令。拼错的表现是"点了没反应"，比报错更难查。"""
    assert desktop.command_for("darwin", "/tmp/x") == ["open", "/tmp/x"]
    assert desktop.command_for("win32", "C:/x") == ["explorer", "C:/x"]
    assert desktop.command_for("linux", "/tmp/x") == ["xdg-open", "/tmp/x"]


def test_missing_directory_is_reported_not_raised(tmp_path) -> None:  # noqa: ANN001
    """目录不在本机：返回 `opened: False`，不抛异常。

    报错的表现是"弹一句红字说库坏了"，而真实情况可能只是目录被移走了 ——
    这两种事对用户的下一步完全不同。
    """
    out = desktop.reveal(tmp_path / "nope")
    assert out["opened"] is False
    assert "why" in out


def test_reveal_starts_the_platform_command(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """正常路径上确实去启动那条命令（这里打桩，别真开一个窗口出来）。"""
    started: list[list[str]] = []

    class Fake:
        def __init__(self, cmd, **kwargs):  # noqa: ANN003
            started.append(list(cmd))

    monkeypatch.setattr(desktop.subprocess, "Popen", Fake)
    monkeypatch.setattr(desktop.sys, "platform", "linux")
    monkeypatch.setattr(desktop.os, "name", "posix")

    out = desktop.reveal(tmp_path)

    assert out["opened"] is True
    assert started == [["xdg-open", str(tmp_path)]]


def test_no_shell_so_special_characters_are_plain_arguments(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """命令与参数**分开放**：目录名里的空格、分号、`$` 都只是普通字符。"""
    weird = tmp_path / "a dir; rm -rf $HOME"
    weird.mkdir()
    seen: list[list[str]] = []

    class Fake:
        def __init__(self, cmd, **kwargs):  # noqa: ANN003
            seen.append(list(cmd))

    monkeypatch.setattr(desktop.subprocess, "Popen", Fake)
    desktop.reveal(weird)

    assert seen[0][1] == str(weird)
    assert len(seen[0]) == 2                 # 没有多出任何一段（没被切开）
    assert "shell" not in seen[0]            # 也不是 ["sh", "-c", ...]
