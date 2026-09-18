"""外部工具的取用与自举：下载、校验、解开、用起来。

用户的要求是这层必须**自包含**（或首启取一次），不能靠"这台机器恰好装了"。
所以这里盯的是四条最容易出事的：

* **没钉哈希一律不装**（装一个没校验的可执行文件比"用不了"糟得多）；
* **哈希对不上就丢弃**，并且**不往缓存里落**；
* 解开之后**真的找得到、真的能执行**（zip 不带可执行位那种坑）；
* **只钉过哈希的才预取**，没钉的连网都不试（不然启动时会在没网的地方干等）。

全程用**本机 HTTP 服务**喂一个假组件，不依赖外网。
"""

from __future__ import annotations

import hashlib
import http.server
import io
import socket
import sys
import tarfile
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from api.app import toolchain as tc  # noqa: E402


def _make_archive(name: str, *, flat: bool = False) -> bytes:
    """造一个 tar.gz，里面放一个能跑的假可执行文件。"""
    script = b"#!/bin/sh\necho " + name.encode() + b"\n"
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as box:
        info = tarfile.TarInfo(f"{name}/{'fake' if flat else 'bin/' + 'fake'}")
        info.size = len(script)
        info.mode = 0o755
        box.addfile(info, io.BytesIO(script))
    return buffer.getvalue()


class _Server:
    """一个只服务内存里那份字节的 HTTP 服务（本机端口，用完就关）。"""

    def __init__(self, payload: bytes):
        # 闭包一份给内层的处理器用：嵌套类里看不到外层的 `self`
        body = payload

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):  # noqa: ANN002
                return

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return f"http://127.0.0.1:{self.port}/fake.tar.gz"

    def __exit__(self, *_exc):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture()
def fake(monkeypatch, tmp_path: Path):
    """一个假组件 + 一份临时清单 + 一个临时缓存目录。"""
    payload = _make_archive("faketool")
    monkeypatch.setitem(
        tc.COMPONENTS,
        "faketool",
        {"why": "测试用", "binary": "fake"},
    )
    monkeypatch.setattr(tc, "MANIFEST_FILE", tmp_path / "toolchain.json")
    monkeypatch.setattr(tc, "VENDOR_DIR", tmp_path / "vendor")
    monkeypatch.setattr(tc, "cache_dir", lambda: tmp_path / "cache" / "tools")
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))   # 系统里"没有"这个工具
    return {"payload": payload, "sha": hashlib.sha256(payload).hexdigest(), "root": tmp_path}


def _write_manifest(url: str, sha: str, *, name: str = "faketool") -> None:
    import json

    tc.MANIFEST_FILE.write_text(
        json.dumps({name: {tc.platform_key(): {"url": url, "sha256": sha}}}), encoding="utf-8"
    )


def test_unpinned_component_is_never_installed(fake):
    """清单里没钉地址与哈希 → 明确拒绝，并且**连网都不试**。"""
    path, why = tc.ensure("faketool")
    assert path is None
    assert "还没钉" in why or "没有" in why
    assert not tc.cache_dir().exists(), "不该有任何东西落到缓存里"

    assert tc.prefetch() == [], "没钉的不预取"


def test_download_verify_unpack_and_use(fake):
    """钉好哈希之后：取下来、校验通过、解开、找得到、能执行。"""
    with _Server(fake["payload"]) as url:
        _write_manifest(url, fake["sha"])
        path, why = tc.ensure("faketool")

    assert path is not None, why
    assert path.is_file()
    assert path.stat().st_mode & 0o100, "要能执行（zip 那条路不带可执行位，得自己补）"
    assert "echo faketool" in path.read_text(encoding="utf-8")

    # 再问一次：这次走缓存，不再下载
    again, source = tc.resolve("faketool")
    assert again == path
    assert source == "本机缓存"


def test_bad_hash_is_discarded_and_nothing_lands(fake):
    """哈希对不上 → 丢弃，缓存里什么都不留。"""
    with _Server(fake["payload"]) as url:
        _write_manifest(url, "0" * 64)
        path, why = tc.ensure("faketool")

    assert path is None
    assert "哈希对不上" in why
    assert not (tc.cache_dir() / "faketool").exists(), "对不上的包不该解开"
    leftovers = list(tc.cache_dir().glob("**/*")) if tc.cache_dir().exists() else []
    assert [one for one in leftovers if one.is_file()] == [], "临时文件也要清掉"


def test_flat_archive_is_still_found(monkeypatch, tmp_path: Path):
    """包里没有 `bin/` 那一层也要找得到（不同发行版的结构不一样）。"""
    payload = _make_archive("flattool", flat=True)
    monkeypatch.setitem(tc.COMPONENTS, "flattool", {"why": "测试用", "binary": "fake"})
    monkeypatch.setattr(tc, "MANIFEST_FILE", tmp_path / "toolchain.json")
    monkeypatch.setattr(tc, "VENDOR_DIR", tmp_path / "vendor")
    monkeypatch.setattr(tc, "cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))

    with _Server(payload) as url:
        _write_manifest(url, hashlib.sha256(payload).hexdigest(), name="flattool")
        path, why = tc.ensure("flattool")

    assert path is not None, why
    assert path.parent.name == "flattool", "平的包就落在组件目录下"


def test_system_install_wins_and_needs_no_download(monkeypatch, tmp_path: Path):
    """系统里已经装了就用它 —— 不折腾、不下载。"""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    tool = bindir / "fake"
    tool.write_text("#!/bin/sh\necho system\n", encoding="utf-8")
    tool.chmod(0o755)
    monkeypatch.setitem(tc.COMPONENTS, "systool", {"why": "测试用", "binary": "fake"})
    monkeypatch.setattr(tc, "VENDOR_DIR", tmp_path / "vendor")
    monkeypatch.setattr(tc, "cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setenv("PATH", str(bindir))

    path, source = tc.resolve("systool")
    assert path == tool
    assert source == "系统"


def test_vendor_copy_is_used_before_downloading(monkeypatch, tmp_path: Path):
    """仓库自带那一份优先于下载（开发机与离线开发靠它）。"""
    vendor = tmp_path / "vendor" / "vendtool" / "bin"
    vendor.mkdir(parents=True)
    tool = vendor / "fake"
    tool.write_text("#!/bin/sh\necho vendored\n", encoding="utf-8")
    monkeypatch.setitem(tc.COMPONENTS, "vendtool", {"why": "测试用", "binary": "fake"})
    monkeypatch.setattr(tc, "VENDOR_DIR", tmp_path / "vendor")
    monkeypatch.setattr(tc, "cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))

    path, source = tc.resolve("vendtool")
    assert path == tool
    assert source == "随仓库自带"


def test_status_says_why_so_the_ui_can_explain():
    """状态要能说出"缺什么、缺的是干什么用的" —— 界面拿它解释空白。"""
    state = tc.status()
    assert state["platform"]
    names = {one["name"] for one in state["components"]}
    assert {"pdftotext", "pandoc"} <= names
    for one in state["components"]:
        assert one["why"], "每个组件都要有一句'它是干什么的'"
        assert "pinned" in one and "available" in one
