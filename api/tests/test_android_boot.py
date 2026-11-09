"""Android 启动器里那些**只在手机上成立**的约定。

这一层不在桌面跑（`android_boot` 由 Chaquopy 调），但它做的事会直接决定
手机上的功能有没有：TeX 引擎的载荷改名就是其中之一。用例盯的是
「改名的规则」本身 —— 打包阶段做了 `.gz → .gzz`，启动时就得一个不差地改回来。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

BOOT_FILE = Path(__file__).resolve().parents[2] / "mobile" / "python_src" / "android_boot.py"


def _boot():  # noqa: ANN202 —— 模块对象，不引类型只为少一层依赖
    """按路径加载启动器（它在 `mobile/` 下，不在 `api` 的 import 路径里）。"""
    spec = importlib.util.spec_from_file_location("qf_android_boot", BOOT_FILE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_tikzjax_payloads_are_renamed_back(tmp_path) -> None:  # noqa: ANN001
    """`.gzz` → `.gz`，包括 `tex_files/` 下面的那一大批。

    为什么必须一个不差：引擎是 `fetch('core.dump.gz')` 与
    `fetch('tex_files/<名>.gz')`，少一个就是 404，那一张图直接 broken。
    """
    tikz = tmp_path / "assets" / "tikzjax"
    tex_files = tikz / "tex_files"
    tex_files.mkdir(parents=True)
    (tikz / "core.dump.gzz").write_bytes(b"dump")
    (tikz / "tex.wasm.gzz").write_bytes(b"wasm")
    (tex_files / "circuitikz.sty.gzz").write_bytes(b"sty")

    detail = _boot().restore_tikzjax_gz(tmp_path)

    assert "3" in detail
    assert (tikz / "core.dump.gz").is_file()
    assert (tikz / "tex.wasm.gz").is_file()
    assert (tex_files / "circuitikz.sty.gz").is_file()
    assert not list(tmp_path.rglob("*.gzz"))


def test_restoring_is_idempotent(tmp_path) -> None:  # noqa: ANN001
    """已经恢复过（桌面形态 / 第二次启动）就什么也不做。"""
    tikz = tmp_path / "assets" / "tikzjax"
    tikz.mkdir(parents=True)
    (tikz / "core.dump.gz").write_bytes(b"dump")

    detail = _boot().restore_tikzjax_gz(tmp_path)

    assert "0" in detail
    assert (tikz / "core.dump.gz").is_file()


def test_missing_tikzjax_directory_is_not_an_error(tmp_path) -> None:  # noqa: ANN001
    """没有这个目录（开发调试 / 前端没带 TikZJax）不该让启动失败。"""
    detail = _boot().restore_tikzjax_gz(tmp_path)
    assert "跳过" in detail
