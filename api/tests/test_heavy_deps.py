"""重型运行组件（Pyodide）的取用与投递：**包里没有它**，缓存与校验必须可靠。

这一层是"轻量分发"的支点：它不在安装包里（76M），而是首启取一次进本机缓存。
所以它出错的方式很难看 —— 下载了但校验不了、或者路径能穿出去、或者
"没准备好"被当成"准备好了"从而白屏。下面这些用例盯的就是这几件事。
"""

from __future__ import annotations

import json

from app import heavy_deps


def test_manifest_lists_the_entry_and_hashes() -> None:
    """清单必须跟着仓库走，且每条都是 64 位十六进制。

    清单不在的话，"首启下载"会退化成"每次都下"（校验不了就等于不校验），
    所以它必须是被提交的产物，而不是运行时生成的东西。
    """
    files = heavy_deps.manifest()
    assert files, "build/pyodide-manifest.json 不在 —— 先跑 python3 build/package.py manifest"
    assert heavy_deps.ENTRY in files
    for name, digest in files.items():
        assert len(digest) == 64, f"{name} 的哈希不像 sha256"
        assert all(char in "0123456789abcdef" for char in digest), f"{name} 的哈希不是十六进制"


def test_ready_requires_a_matching_hash(tmp_path) -> None:  # noqa: ANN001
    """只有文件名对不算准备好 —— 内容必须与清单一致。

    这正是"下载被中间人换过 / 上次下了一半"的兜底：文件在、名字对、大小可能也对，
    但哈希不对就不该被当成可用。
    """
    expected = heavy_deps.manifest()
    assert expected, "清单缺失时这条用例没有意义"

    # 空目录：没准备好
    assert heavy_deps.is_ready(tmp_path) is False

    # 放一个**内容不对**的同名文件：还是没准备好
    (tmp_path / heavy_deps.ENTRY).write_bytes(b"// not the real pyodide")
    assert heavy_deps.is_ready(tmp_path) is False


def test_ready_when_every_file_matches(tmp_path) -> None:  # noqa: ANN001
    """清单里的每个文件都按内容摆好 → 就算就绪（不联网也能判定）。"""
    expected = heavy_deps.manifest()
    assert expected, "清单缺失时这条用例没有意义"

    # 真文件太大，这里用"改清单"的方式造一个能对上的小世界
    (tmp_path / heavy_deps.ENTRY).write_bytes(b"stub")
    digest = heavy_deps._sha256(tmp_path / heavy_deps.ENTRY)  # noqa: SLF001
    original = heavy_deps.manifest
    heavy_deps.manifest = lambda: {heavy_deps.ENTRY: digest}  # type: ignore[assignment]
    try:
        assert heavy_deps.is_ready(tmp_path) is True
    finally:
        heavy_deps.manifest = original  # type: ignore[assignment]


def test_file_path_refuses_to_walk_out_of_the_cache() -> None:
    """只认清单里的**文件名**，不许带路径 —— 这个接口是给浏览器取的。"""
    for name in ("../../etc/passwd", "..\\..\\windows\\system32", "sub/pyodide.js", "", "nope.js"):
        assert heavy_deps.file_path(name) is None, f"{name!r} 不该被接受"


def test_asset_route_404s_for_names_it_does_not_own(client) -> None:  # noqa: ANN001
    """接口面：拿不到就 404（而不是 500、也不是拿别的文件糊弄过去）。"""
    for name in ("nope.js", "index.html", "pyodide.js.bak"):
        response = client.get(f"/assets/pyodide/{name}")
        assert response.status_code == 404


def test_settings_do_not_prefetch_during_tests() -> None:
    """用例里不许去联网取 76M 的运行时（conftest 把它关掉了）。

    这条看着琐碎，但它防的是一件很烦的事：跑一次测试就下载几十兆，
    而且是在 CI 或别人机器上悄悄发生。
    """
    from app.config import get_settings

    assert get_settings().heavy_prefetch is False


def test_manifest_file_is_valid_json() -> None:
    """清单本身要能被解析（它是运行时读的，坏了就等于没有）。"""
    payload = json.loads(heavy_deps.MANIFEST_FILE.read_text(encoding="utf-8"))
    assert payload["files"]
    assert payload["bytes"] > 0


def test_manifest_path_can_be_pointed_elsewhere(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """Android 上清单不跟 `ROOT` 走。

    Chaquopy 把源码编进 `app.imy`，`__file__` 的层级与仓库不同（按 `parents[2]`
    推出来的是 `files/chaquopy/`，清单却在 `app.imy/` 里），所以启动器用
    `QF_HEAVY_MANIFEST` 指到它拷出来的那份。
    """
    custom = tmp_path / "pyodide-manifest.json"
    custom.write_text(
        json.dumps({"files": {heavy_deps.ENTRY: "a" * 64}, "bytes": 1}), encoding="utf-8"
    )
    monkeypatch.setenv(heavy_deps.MANIFEST_ENV, str(custom))
    assert heavy_deps.manifest_path() == custom
    assert heavy_deps.manifest() == {heavy_deps.ENTRY: "a" * 64}


def test_missing_manifest_records_a_reason_and_backs_off(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """没有清单时：不下载、留下原因、短时间内不重试。

    三件事缺一不可。少了最后一条，用户每问一次"跑一下这段"就重起一轮下载，
    界面上看到的永远是"已经在后台开始取了"—— 2026-11-09 之前手机端的实际症状。
    """
    monkeypatch.setattr(heavy_deps, "_failure", {"detail": "", "at": 0.0})
    monkeypatch.setattr(heavy_deps, "cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setattr(heavy_deps, "_copy_from_vendor", lambda _say: False)
    monkeypatch.setenv(heavy_deps.MANIFEST_ENV, str(tmp_path / "missing.json"))

    ok, detail = heavy_deps.ensure(allow_download=True)
    assert ok is False
    assert "清单" in detail
    assert "清单" in heavy_deps.last_failure()
    # 退避期内不再起后台任务
    assert heavy_deps.start_prefetch() is False


def test_data_dir_is_out_of_the_bundle() -> None:
    """打包后数据目录：在**用户主目录**下（`~/quizforge`）。

    不能用 `<根>/data` —— 单文件模式里那个"根"是 `%TEMP%`，用户的库会住在
    临时目录里（既不好找，也可能被清理工具收走）。

    2026-11-08：分发通道删除后只有一个位置；老包留下的 `~/quizforge-beta`
    会被一次性搬到这儿（见 `frozen_data_dir`）。
    """
    from pathlib import Path

    from app import config

    assert config.frozen_data_dir() == Path.home() / "quizforge"


# 2026-11-07：内测通道（`beta_active` / `is_beta` / `ai_beta_enabled`）整块删除。
# 2026-11-08：分发通道（`beta` / `release`）也删除 —— 只有一个版本，`frozen_data_dir()`
# 不再收参数、包里也不再带 `channel.json`。原来那两条各自验通道的用例随之作废。
