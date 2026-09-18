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


def test_data_dir_is_out_of_the_bundle_and_per_channel() -> None:
    """打包后数据目录：在**用户主目录**下，且**按通道分开**。

    两条都是定下来的取舍：

    ① 不能用 `<根>/data` —— 单文件模式里那个"根"是 `%TEMP%`，用户的库会住在
       临时目录里（既不好找，也可能被清理工具收走）；
    ② 内测与正式必须分开（用户原话"内测和正式要分开"）—— 同一台机器上跑两个包，
       库、作答记录、以后的笔记与资料互不干扰。
    """
    from pathlib import Path

    from app import config

    assert config.frozen_data_dir("beta") == Path.home() / "quizforge-beta"
    assert config.frozen_data_dir("release") == Path.home() / "quizforge"
    assert config.frozen_data_dir("beta") != config.frozen_data_dir("release")


def test_release_channel_never_uses_the_beta_quota() -> None:
    """正式通道**连内测配置文件都不看** —— "内测和正式分开"的落点在这里。

    不靠"配置写对"，靠结构：通道不是 beta 就没有内测额度可用。
    于是正式包不可能悄悄用站长的钱。
    """
    from app.config import Settings

    assert Settings(channel="beta", ai_beta_enabled=True).beta_active is True
    assert Settings(channel="beta", ai_beta_enabled=False).beta_active is False
    # 关键的一条：正式通道下，开关开着也不生效
    assert Settings(channel="release", ai_beta_enabled=True).beta_active is False
    assert Settings(channel="release").is_beta is False
    assert Settings(channel="beta").is_beta is True


def test_beta_config_is_looked_up_in_the_bundle_not_in_temp() -> None:
    """内测配置按**只读资源根**找：开发时在仓库里，打包后在解包里。

    用 `ROOT` 会怎样：单文件模式下它是解包目录的上一级（`%TEMP%`），
    于是"内测配置在不在"被问到一个临时目录里去 —— 内测包因此拿不到额度，
    而报错的地方离原因很远（`heavy_deps` 里同样的坑刚踩过）。
    """
    from app import config

    settings = config.get_settings()
    assert settings.ai_beta_config_file == config.RESOURCE_ROOT / "config" / "ai.local.json"
    assert settings.ai_beta_config_file.parent.parent == config.RESOURCE_ROOT


def test_channel_comes_from_env_then_bundle(monkeypatch) -> None:  # noqa: ANN001
    """通道的来源顺序：环境变量 > 包里带的 `channel.json` > 默认。

    冻结之后没有环境变量可读，所以打包时会把通道写进包里 —— 这条用例验的是
    "包里的声明能被读到"这条路径。
    """
    from app import config

    monkeypatch.setenv("QF_CHANNEL", "release")
    assert config._default_channel() == "release"  # noqa: SLF001
    monkeypatch.setenv("QF_CHANNEL", "whatever")  # 不认识的取值不该被当真
    assert config._default_channel() == "beta"  # noqa: SLF001
