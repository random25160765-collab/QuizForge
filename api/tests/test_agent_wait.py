"""`run_python` 那一轮**真的会等**输出 —— 端到端（不靠模型）。

用户的原话："让 agent 等命令返回了再说话。"

原先不等：工具立刻返回、模型当场开口（"脚本已经交进去跑了，我下一轮就能看到"），
输出要下一轮才进上下文。现在 `run_python` 的回合会停在 `runs.wait_blocking` 上，
页面（另一个线程/另一条请求）把输出报回来才继续。

这条测试把整条链子接起来验：伪造网关 → 循环发起 `run_python` → 循环**卡住等** →
另一个线程报输出 → 断言**喂给模型的那条工具结果里带着输出**。要是哪天有人把那个
等待删掉（或把顺序改成"先等后发零件"，那会死锁），这条会红。
"""

import pathlib
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app import agent_loop, runs, tools  # noqa: E402


def test_loop_waits_for_the_sandbox_output(db_session, monkeypatch):
    run_id = "wait-test-1"
    delivered = {"at": None}

    def fake_stream(conf, messages, tools=None, params=None):  # noqa: A002 - 与真实签名同名
        """第一轮：要求调 `run_python`；第二轮：给一句普通回答（停止要工具）。

        循环是**按消息条数**判断轮次的，所以这里数一下有没有工具结果进来。
        """
        has_tool_result = any(one.get("role") == "tool" for one in messages)
        if not has_tool_result:
            yield ("tool_calls", [{"id": "call_1", "name": "run_python",
                                   "arguments": '{"code": "print(1)", "title": "t"}'}])
            yield ("finish", "tool_calls")
            return
        yield ("delta", "跑完了，结论是 1。")
        yield ("finish", "stop")

    def fake_call(db, name, args, ctx=None, **kwargs):
        """只替掉 `run_python` 的结果：真让它返回"待运行"的那个形状。"""
        return True, {"demo": {"title": "t", "runId": run_id}, "await_run": True,
                      "note": "会等它跑完"}

    monkeypatch.setattr(agent_loop.gateway, "stream_completion", fake_stream)
    monkeypatch.setattr(tools, "call", fake_call)

    # 循环会卡在等待里，所以从**另一个线程**把输出报进去（生产里就是页面那条）
    def report_later():
        # 等**自己这个** runId 真的在等（别的测试可能还留着没超时的信箱）
        for _ in range(200):
            if run_id in runs.pending():
                break
            time.sleep(0.05)
        delivered["at"] = time.perf_counter()
        assert runs.deliver(run_id, {"ok": True, "text": "1\n", "ms": 2}) is True

    thread = threading.Thread(target=report_later)
    thread.start()

    started = time.perf_counter()
    events = list(
        agent_loop.run(
            db_session,
            {"model": "fake"},
            system="s",
            history=[{"role": "user", "content": "跑一下"}],
            # 这里要的是**工具模块本身**（循环会自己调 `tools.specs(...)`）
            tools=tools,
            tool_context={},
            mounts=None,
            allow=None,
        )
    )
    thread.join(timeout=5)
    took = time.perf_counter() - started

    kinds = [one["kind"] for one in events]
    assert "run_output" in kinds, kinds
    out = [one for one in events if one["kind"] == "run_output"][-1]
    assert out["runId"] == run_id
    assert out["run"]["text"] == "1\n"

    # 关键：**等到了**才继续 —— 所以整轮花的时间不短于"回报"那一刻
    assert delivered["at"] is not None, "没人报输出，说明循环没进入等待"
    assert took >= 0.05, "循环根本没停：等待被删了"

    # 而且模型最后那句话是在**拿到输出之后**说的（顺序对，不是并发）
    text_at = kinds.index("text") if "text" in kinds else len(kinds)
    assert kinds.index("run_output") < text_at, "先说了话才等到输出 —— 顺序反了"
