"""重型运行组件（Pyodide）的取用与投递：**包里没有它**，缓存与校验必须可靠。

这一层是"轻量分发"的支点：它不在安装包里（146M），而是首启取一次进本机缓存。
所以它出错的方式很难看 —— 下载了但校验不了、或者路径能穿出去、或者
"没准备好"被当成"准备好了"从而白屏。下面这些用例盯的就是这几件事。
"""

from __future__ import annotations

import hashlib
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


class _FakeResponse:
    """够 `_download` 用的最小响应：按块吐字节，支持 with。"""

    def __init__(self, payload: bytes) -> None:
        self._left = payload

    def read(self, size: int = -1) -> bytes:
        if not self._left:
            return b""
        chunk, self._left = self._left[:size], self._left[size:]
        return chunk

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


def test_download_reports_progress_for_the_splash_screen(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """首次安装的进度条读的就是 `done/total`（字节）。

    用户 2026-11-10："大的依赖不该让它在背后自己下，在首次安装的时候显示进度条" ——
    所以下载过程中必须能把"下到哪了"说清楚，而不是只有一句"正在取"。
    """
    payload = b"hello world " * 512
    digest = hashlib.sha256(payload).hexdigest()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"files": {"a.bin": digest}, "bytes": len(payload)}), encoding="utf-8"
    )
    monkeypatch.setenv(heavy_deps.MANIFEST_ENV, str(manifest))
    monkeypatch.setattr(heavy_deps, "cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setattr(heavy_deps, "VENDOR_DIR", tmp_path / "no-vendor")
    monkeypatch.setattr(heavy_deps, "_failure", {"detail": "", "at": 0.0})
    monkeypatch.setattr(
        heavy_deps,
        "_progress",
        {"state": "idle", "done": 0, "total": 0, "files": 0, "filesTotal": 0, "detail": ""},
    )
    monkeypatch.setattr(
        heavy_deps.urllib.request, "urlopen", lambda *_a, **_k: _FakeResponse(payload)
    )

    ok, _detail = heavy_deps.ensure(allow_download=True)
    assert ok is True

    state = heavy_deps.progress()
    assert state["state"] == "ok"
    assert state["done"] == len(payload)      # 进度条的分母/分子都按字节
    assert state["total"] == len(payload)
    assert state["files"] == 1
    assert state["filesTotal"] == 1


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


def test_verify_names_the_files_that_do_not_match(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """「重新下载并校验」要能说清楚**缺了哪几个**，而不是只回一句"不完整"。

    文件在、名字对、大小可能也对 —— 下载被打断时就是这种状态。判据只能是哈希。
    """
    payload = b"ok"
    digest = hashlib.sha256(payload).hexdigest()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"files": {"a.bin": digest, "b.bin": digest}, "bytes": 4}), encoding="utf-8"
    )
    monkeypatch.setenv(heavy_deps.MANIFEST_ENV, str(manifest))

    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "a.bin").write_bytes(payload)
    (cache / "b.bin").write_bytes(b"broken")

    report = heavy_deps.verify(cache)
    assert report["ready"] is False
    assert report["missing"] == ["b.bin"]      # 坏掉的那个，而不是齐的那份
    assert report["missingCount"] == 1
    assert report["checked"] == 1
    assert report["total"] == 2


def test_repair_says_so_when_the_cache_is_complete(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """缓存完整时按下去不该再下一轮 —— 直接给结论（这是"校验"那一半）。"""
    payload = b"ok"
    digest = hashlib.sha256(payload).hexdigest()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"files": {"a.bin": digest}, "bytes": len(payload)}), encoding="utf-8"
    )
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "a.bin").write_bytes(payload)
    monkeypatch.setenv(heavy_deps.MANIFEST_ENV, str(manifest))
    monkeypatch.setattr(heavy_deps, "cache_dir", lambda: cache)
    monkeypatch.setattr(heavy_deps, "_prefetching", False)
    monkeypatch.setattr(
        heavy_deps,
        "_progress",
        {"state": "idle", "done": 0, "total": 0, "files": 0, "filesTotal": 0, "detail": ""},
    )

    report = heavy_deps.repair()
    assert report["ready"] is True
    assert report["started"] is False
    assert report["state"] == "ok"
    assert heavy_deps.progress()["state"] == "ok"


def test_repair_restarts_the_download_when_files_are_missing(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """缺文件时按下去 = 起一轮后台补齐（`force` 绕过失败退避）。"""
    payload = b"ok"
    digest = hashlib.sha256(payload).hexdigest()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"files": {"a.bin": digest, "b.bin": digest}, "bytes": 4}), encoding="utf-8"
    )
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "a.bin").write_bytes(payload)     # b.bin 缺失
    monkeypatch.setenv(heavy_deps.MANIFEST_ENV, str(manifest))
    monkeypatch.setattr(heavy_deps, "cache_dir", lambda: cache)
    monkeypatch.setattr(heavy_deps, "_prefetching", False)
    monkeypatch.setattr(
        heavy_deps,
        "_progress",
        {"state": "idle", "done": 0, "total": 0, "files": 0, "filesTotal": 0, "detail": ""},
    )
    calls: list[dict] = []
    monkeypatch.setattr(heavy_deps, "start_prefetch", lambda **kw: calls.append(kw) or True)

    report = heavy_deps.repair()
    assert report["started"] is True
    assert report["state"] == "running"
    assert report["missing"] == ["b.bin"]
    assert calls and calls[0].get("force") is True
    assert heavy_deps.progress()["state"] == "running"


def test_repair_endpoint_reports_what_it_found(client, monkeypatch) -> None:  # noqa: ANN001
    """接口面：按下去要给出**结论**，不能只回一句"收到了"。

    `start_prefetch` 在这里被挡住 —— 用例绝不该真的去下 146M。
    """
    from app import heavy_deps

    monkeypatch.setattr(
        heavy_deps,
        "verify",
        lambda directory=None: {
            "ready": False,
            "checked": 0,
            "total": 2,
            "manifest": 2,
            "missingCount": 2,
            "missing": ["a.bin", "b.bin"],
            "detail": "2 个文件缺失或损坏",
            "cache": "/tmp/nowhere",
        },
    )
    monkeypatch.setattr(heavy_deps, "prefetching", lambda: False)
    monkeypatch.setattr(heavy_deps, "start_prefetch", lambda **_kw: False)

    response = client.post("/api/heavy/repair")
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is False
    assert body["missingCount"] == 2
    # 起不来时不许谎报"正在下载"
    assert body["state"] == "failed"


# 2026-11-07：内测通道（`beta_active` / `is_beta` / `ai_beta_enabled`）整块删除。
# 2026-11-08：分发通道（`beta` / `release`）也删除 —— 只有一个版本，`frozen_data_dir()`
# 不再收参数、包里也不再带 `channel.json`。原来那两条各自验通道的用例随之作废。
