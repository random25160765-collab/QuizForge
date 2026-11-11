"""一轮里的多个**只读**调用并发跑（2026-11-11 用户："查询和搜索，还是只能串行"）。

模型常一口气查五六处地方（材料、笔记、知识点），它们之间没有依赖：串行只是把
五六个往返叠在一起。这条测试钉四件事：

    * 两个 `read` 调用在时间上**真的重叠**（不是排队），且整轮耗时接近一个；
    * `write` 不并发 —— 它改库，顺序可预期比快重要（并发只开给只读那一档）；
    * **同一个调用只做一次**（模型常把同一个网址读两遍）；
    * 并发路数别卡在 4 —— 发 5 个时第 5 个不能排在队尾（排队时间还算进它自己的 `ms`）。
"""

import json
import threading
import time

from app import agent_loop, tools


def _fake_stream(turn_calls):  # noqa: ANN001, ANN202
    """第一轮：要求调这批工具；第二轮：给一句普通回答（停止要工具）。"""

    def stream(conf, messages, tools=None, params=None):  # noqa: A002, ANN001
        if not any(one.get("role") == "tool" for one in messages):
            yield ("tool_calls", turn_calls)
            yield ("finish", "tool_calls")
            return
        yield ("delta", "好了")
        yield ("finish", "stop")

    return stream


def _spy(spans: list, lock: threading.Lock, delay: float):  # noqa: ANN001, ANN202
    def fake_call(db, name, args, ctx=None, **kwargs):  # noqa: ANN001, ANN002, ANN003
        started = time.perf_counter()
        time.sleep(delay)
        with lock:
            spans.append((started, time.perf_counter()))
        return True, {"items": []}

    return fake_call


def test_two_read_calls_in_one_turn_overlap(db_session, monkeypatch) -> None:  # noqa: ANN001
    spans: list = []
    lock = threading.Lock()

    monkeypatch.setattr(
        agent_loop.gateway,
        "stream_completion",
        _fake_stream(
            [
                {"id": "c1", "name": "list_materials", "arguments": '{"q": "a"}'},
                {"id": "c2", "name": "list_materials", "arguments": '{"q": "b"}'},
            ]
        ),
    )
    monkeypatch.setattr(tools, "call", _spy(spans, lock, 0.3))

    begun = time.perf_counter()
    events = list(
        agent_loop.run(
            db_session,
            {"model": "deepseek-chat", "maxContextTokens": 20000},
            system="",
            history=[{"role": "user", "content": "查两处"}],
            tools=tools,
        )
    )
    elapsed = time.perf_counter() - begun

    assert [one["kind"] for one in events].count("tool_result") == 2
    assert len(spans) == 2

    (start_a, end_a), (start_b, _end_b) = sorted(spans)
    assert start_b < end_a, "两次调用没有重叠 —— 还是串行的"
    assert elapsed < 0.55, f"两个 0.3s 的只读调用花了 {elapsed:.2f}s —— 没有并发"


def test_a_write_call_keeps_the_turn_serial(db_session, monkeypatch) -> None:  # noqa: ANN001
    """写操作**不并发**：整轮里只要有一个 `write`，这一轮就回到串行。"""
    spans: list = []
    lock = threading.Lock()

    monkeypatch.setattr(
        agent_loop.gateway,
        "stream_completion",
        _fake_stream(
            [
                {"id": "c1", "name": "list_materials", "arguments": "{}"},
                {"id": "c2", "name": "edit_memory", "arguments": "{}"},
            ]
        ),
    )
    monkeypatch.setattr(tools, "call", _spy(spans, lock, 0.25))

    list(
        agent_loop.run(
            db_session,
            {"model": "deepseek-chat", "maxContextTokens": 20000},
            system="",
            history=[{"role": "user", "content": "查一处、改一处"}],
            tools=tools,
        )
    )

    assert len(spans) == 2
    (_start_a, end_a), (start_b, _end_b) = sorted(spans)
    assert start_b >= end_a, "写操作混进来时仍然并发了 —— 那会让改库的顺序不可预期"


def test_the_same_call_twice_runs_once(db_session, monkeypatch) -> None:  # noqa: ANN001
    """模型一轮里常把同一个网址读两遍（它不记得刚发过）—— 抓一次就够。

    界面仍要收到**两条** `tool_result`（按 callId 各回各的），只是背后只跑了一次。
    """
    spans: list = []
    lock = threading.Lock()
    same = {"name": "read_web_page", "arguments": '{"url": "https://example.com/a"}'}

    monkeypatch.setattr(
        agent_loop.gateway,
        "stream_completion",
        _fake_stream([dict(same, id="c1"), dict(same, id="c2")]),
    )
    monkeypatch.setattr(tools, "call", _spy(spans, lock, 0.2))

    events = list(
        agent_loop.run(
            db_session,
            {"model": "deepseek-chat", "maxContextTokens": 20000},
            system="",
            history=[{"role": "user", "content": "读这一页"}],
            tools=tools,
        )
    )

    assert [one["callId"] for one in events if one["kind"] == "tool_result"] == ["c1", "c2"]
    assert len(spans) == 1, "同一个调用跑了两遍 —— 白等一次网络"


def test_five_read_calls_do_not_queue_behind_four_slots(db_session, monkeypatch) -> None:  # noqa: ANN001
    """并发路数别卡在 4：实测 5 个调用时第 5 个排队 15 秒，而它自己的 `ms` 把排队也算进去。"""
    spans: list = []
    lock = threading.Lock()
    calls = [
        {"id": "c" + str(i), "name": "list_materials", "arguments": json.dumps({"q": i})}
        for i in range(5)
    ]

    monkeypatch.setattr(agent_loop.gateway, "stream_completion", _fake_stream(calls))
    monkeypatch.setattr(tools, "call", _spy(spans, lock, 0.3))

    begun = time.perf_counter()
    list(
        agent_loop.run(
            db_session,
            {"model": "deepseek-chat", "maxContextTokens": 20000},
            system="",
            history=[{"role": "user", "content": "查五处"}],
            tools=tools,
        )
    )
    elapsed = time.perf_counter() - begun

    assert len(spans) == 5
    first_end = min(end for _start, end in spans)
    assert max(start for start, _end in spans) < first_end - 0.05, "有一个在别人结束后才开始 —— 排队了"
    assert elapsed < 0.55, f"五个 0.3s 的只读调用花了 {elapsed:.2f}s —— 路数不够"
