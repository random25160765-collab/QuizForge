"""沙箱画的图要**真的到模型眼前**，而且只到一次。

用户的原话："你要保证 matplotlib 的图能被 llm 读到 —— 它是有读图能力的。"

原先图只到面板就断了：模型那边只有一个"运行产出的图"的空名字，于是它只能说
"我这边看不到图"。现在这条路是：

    shell（savefig 收走）→ 回传 images → 跑那一轮等在 `runs` 上 →
    工具结果里附一条**带 image_url 的 user 消息**（协议上 `role:"tool"` 带不了图）

两条边界一并钉住：**读不了图的模型不发**（给上游塞 image_url 会 400）、
**同一张图只发一次**（base64 一张上万 token，随历史重放纯属白烧）。
"""

import copy
import pathlib
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app import agent_loop, runs, tools  # noqa: E402

TINY_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _image_messages(messages: list[dict]) -> list[dict]:
    """把带图的消息挑出来（图在 content 的 image_url 部件里）。"""
    return [
        one
        for one in messages
        if isinstance(one.get("content"), list)
        and any(
            isinstance(part, dict) and part.get("type") == "image_url"
            for part in one["content"]
        )
    ]


def test_the_figure_reaches_the_model_once(db_session, local_user, monkeypatch):
    run_id = "img-test-1"
    snapshots: list[list[dict]] = []

    def fake_stream(conf, messages, tools=None, params=None):  # noqa: A002 - 与真实签名同名
        # 深拷一份：`_strip_images` 是**原地**改消息的，浅拷会看到"改完之后"的样子
        snapshots.append(copy.deepcopy(messages))
        if not any(one.get("role") == "tool" for one in messages):
            yield (
                "tool_calls",
                [
                    {
                        "id": "c1",
                        "name": "run_python",
                        "arguments": '{"code": "import matplotlib.pyplot as plt; plt.plot([1,2])", "title": "t"}',
                    }
                ],
            )
            yield ("finish", "tool_calls")
            return
        yield ("delta", "图我看到了。")
        yield ("finish", "stop")

    def fake_call(db, user, name, args, ctx, **kwargs):  # noqa: ANN001
        """只替掉 `run_python` 的结果：让它返回"待运行"那个形状。

        必须替：真 `run_python` 会生成**它自己的** runId，而这条测试要报的是
        `run_id`（另一个线程往那个信箱里送图）。
        """
        return True, {
            "demo": {"title": "t", "runId": run_id},
            "await_run": True,
            "note": "会等它跑完",
        }

    monkeypatch.setattr(agent_loop.gateway, "stream_completion", fake_stream)
    monkeypatch.setattr(tools, "call", fake_call)
    # 读图能力：这一条用的是能读图的模型
    monkeypatch.setattr(agent_loop.gateway, "model_reads_images", lambda model: True)

    def report_later():
        for _ in range(200):
            if run_id in runs.pending():
                break
            time.sleep(0.05)
        runs.deliver(
            run_id,
            {"ok": True, "text": "画好了\n", "ms": 5, "images": [TINY_PNG]},
        )

    thread = threading.Thread(target=report_later)
    thread.start()
    try:
        events = list(
            agent_loop.run(
                db_session,
                local_user,
                {"apiKey": "x", "model": "vision-fake"},
                system="系统",
                history=[{"role": "user", "content": "画一张"}],
                tools=tools,
                max_turns=4,
            )
        )
    finally:
        thread.join(timeout=3)

    kinds = [one.get("kind") for one in events]
    assert "tool_result" in kinds, kinds

    # 1) 图确实进了**发给模型的消息**（第二条快照 = 工具结果之后那一轮）
    assert len(snapshots) >= 2, "该至少调上游两次（派活 → 看图说话）"
    with_image = _image_messages(snapshots[1])
    assert with_image, "图没送到模型眼前 —— 它只能干瞪眼说看不到"
    assert str(with_image[0]["content"][1]["image_url"]["url"]).startswith("data:image/png;base64,")
    # 我们自己的标记不许发给上游（`_fresh` 是给 _strip_images 看的）
    assert "_fresh" not in with_image[0], "内部标记不该出现在请求里"


def test_a_model_that_cannot_see_gets_no_image(db_session, local_user, monkeypatch):
    """读不了图的模型**不发图**：给上游塞 image_url 会直接 400。"""
    snapshots: list[list[dict]] = []

    def fake_stream(conf, messages, tools=None, params=None):  # noqa: A002
        snapshots.append(copy.deepcopy(messages))
        if not any(one.get("role") == "tool" for one in messages):
            yield (
                "tool_calls",
                [{"id": "c1", "name": "run_python", "arguments": '{"code": "print(1)"}'}],
            )
            yield ("finish", "tool_calls")
            return
        yield ("delta", "好。")
        yield ("finish", "stop")

    run_id = "img-test-2"

    def fake_call(db, user, name, args, ctx, **kwargs):  # noqa: ANN001
        return True, {
            "demo": {"title": "t", "runId": run_id},
            "await_run": True,
            "note": "会等它跑完",
        }

    monkeypatch.setattr(agent_loop.gateway, "stream_completion", fake_stream)
    monkeypatch.setattr(tools, "call", fake_call)
    monkeypatch.setattr(agent_loop.gateway, "model_reads_images", lambda model: False)

    def report_later():
        for _ in range(200):
            if run_id in runs.pending():
                break
            time.sleep(0.05)
        runs.deliver(run_id, {"ok": True, "text": "1\n", "ms": 2, "images": [TINY_PNG]})

    thread = threading.Thread(target=report_later)
    thread.start()
    try:
        list(
            agent_loop.run(
                db_session,
                local_user,
                {"apiKey": "x", "model": "no-vision-fake"},
                system="系统",
                history=[{"role": "user", "content": "跑一段"}],
                tools=tools,
                max_turns=4,
            )
        )
    finally:
        thread.join(timeout=3)

    assert snapshots, "该调过上游"
    assert all(not _image_messages(one) for one in snapshots), "读不了图就不该发图"


def test_old_images_are_replaced_before_the_next_request():
    """同一张图只发一次：下一轮它就该变成一句"这里原本有一张图"。

    而且**只动沙箱拍的那种**（`name == sandbox-figure`）—— 附件的图是用户发的、
    是历史的一部分，每轮都该在（那条路另有用例钉着，不能被这里误伤）。
    """
    messages = [
        {"role": "user", "content": "画一张"},
        {
            "role": "user",
            "name": "sandbox-figure",
            "_fresh": True,
            "content": [
                {"type": "text", "text": "（上面那次运行的图在下面）"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ],
        },
        # 附件：**不带**那个记号，任何一轮都该原样发
        {
            "role": "user",
            "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,BBBB"}}],
        },
    ]

    # 第一次：当轮刚拍的，留着（顺手把标记摘掉 —— 上游不该看到我们的字段）
    agent_loop._strip_images(messages)
    assert _image_messages(messages), "当轮那张要留着给模型看"
    assert "_fresh" not in messages[1], "标记要在发出去之前摘掉"
    assert messages[1].get("name") == "sandbox-figure", "记号还在（下一轮靠它认出来）"

    # 第二次：它已经旧了，换成一句说明；附件那条**一点不动**
    agent_loop._strip_images(messages)
    sandbox = [one for one in messages if one.get("name") == "sandbox-figure"]
    assert sandbox == [], "处理过就把记号清掉（幂等，不会再包一层）"
    assert not _image_messages(messages[:2]), "旧图不该随历史重放（一张上万 token）"
    assert any(
        "原本有一张图" in str(part.get("text") or "")
        for part in messages[1]["content"]
        if isinstance(part, dict)
    )
    assert _image_messages([messages[2]]), "附件的图不受影响（那是用户发的、是历史）"
