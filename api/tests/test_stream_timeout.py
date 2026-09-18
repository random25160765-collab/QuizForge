"""流式读超时：必须**快速**失败，而且要分清「没响应」与「吐一半断了」。

两条契约：

* 读超时是「两次读之间」的上限，不是总时长 —— 所以它天然就是首字节超时。
  2026-09-17 实测：把整份预算（90 秒）当读超时用，通道无响应时界面要转满一分半
  才报错，而这期间界面上写的是「正在生成…」。
* 两种超时的下一步动作不同：一个字都没收到该去查通道/网络，吐到一半断了直接重试即可。
  文案混在一起，会把排查从「看一眼」变成「猜」。
"""

from __future__ import annotations

import socket
import threading
import time

import pytest

from app import ai_gateway


def _silent_upstream(chunks: list[bytes] | None = None) -> int:
    """假上游：收下请求、可选地吐几块，然后闭嘴（**不**关闭连接）。

    不关连接是关键 —— 关了就是正常的流结束，测不到读超时。
    """
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def serve() -> None:
        conn, _ = srv.accept()
        try:
            conn.recv(1 << 16)
            conn.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/event-stream\r\n"
                b"Transfer-Encoding: chunked\r\n\r\n"
            )
            for chunk in chunks or []:
                conn.sendall(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
            time.sleep(60)
        except OSError:
            pass

    threading.Thread(target=serve, daemon=True).start()
    return port


def _conf(port: int) -> dict:
    return {
        "apiKey": "sk-test",
        "baseUrl": f"http://127.0.0.1:{port}/v1",
        "model": "test-model",
        "timeoutMs": 90000,  # 故意给足：失败必须是读超时造成的，不是预算用尽
    }


def test_no_response_fails_fast(monkeypatch) -> None:
    """一个字都不回：应在读超时处失败，而不是等满整份预算。"""
    monkeypatch.setattr(ai_gateway, "STREAM_READ_TIMEOUT_S", 0.5)

    started = time.perf_counter()
    with pytest.raises(ai_gateway.UpstreamError) as err:
        list(ai_gateway.stream_completion(_conf(_silent_upstream()), [{"role": "user", "content": "hi"}]))
    waited = time.perf_counter() - started

    assert err.value.kind == "timeout"
    assert "没有任何响应" in str(err.value), str(err.value)
    assert waited < 5, f"等满预算了：{waited:.1f}s"
    # detail 留空即回落到文案：界面显示的是 detail，不能让它出 httpx 的英文原文
    assert err.value.detail == str(err.value), err.value.detail


def test_midstream_cutoff_reports_how_much_arrived(monkeypatch) -> None:
    """吐了字之后卡住：文案要说清已经收到多少，好判断是重试还是查通道。"""
    monkeypatch.setattr(ai_gateway, "STREAM_READ_TIMEOUT_S", 0.5)

    delta = 'data: {"choices":[{"delta":{"content":"你好"}}]}\n\n'.encode()
    events = []
    with pytest.raises(ai_gateway.UpstreamError) as err:
        for event in ai_gateway.stream_completion(
            _conf(_silent_upstream([delta])), [{"role": "user", "content": "hi"}]
        ):
            events.append(event)

    assert ("delta", "你好") in events, "已吐出的部分必须先交给调用方"
    assert "没有继续返回" in str(err.value), str(err.value)
    assert "已收到 2 字" in str(err.value), str(err.value)
    assert err.value.detail == str(err.value), err.value.detail
