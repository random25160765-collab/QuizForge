"""对话内核：会话、消息树、流式。

这一版最要紧的三件事，都用测试钉住：

* **归属**：别人的会话按 404 处理（不是 403 —— 403 等于承认「这个 id 存在」）
* **中断不丢产出**：上游出错时已经吐出来的字必须留在库里，
  而不是随着失败的响应一起消失（那十几秒是用户等着看着的）
* **再生成是分支**：同一个问题下挂第二个回答，界面只画当前那条线

供应商一律被打桩：这些测试不该因为「今天有没有网」而红。
"""

from __future__ import annotations

import json

import pytest
import uuid
from datetime import datetime, timedelta, timezone

from app import agent_loop, ai_gateway, parts
from app.models import Material

PASSWORD = "password-1234"


# ------------------------------------------------------------------ 脚手架


def _register(client, *args, **kwargs) -> str:  # noqa: ANN001
    """不再需要做什么 —— 账号系统已整体拆除（2026-09-22，见 `app/models.py` 顶部）。

    保留这个空函数只是为了不动几十个调用点：它在用例里当"开工准备"用，
    而现在没有任何准备工作要做（数据隔离由 conftest 的 autouse fixture 负责）。
    """
    return ""


def _headers(client) -> dict:  # noqa: ANN001
    # CSRF 随账号面一起删掉了；留着是为了不动调用点
    return {}


def _set_ai(client, **conf) -> None:  # noqa: ANN001
    """把 AI 配置写进该用户的设置里（与设置面板走同一条通道）。"""
    resp = client.post(
        "/api/progress/sync",
        json={"settings": {"ai": conf}, "settingsRev": 1},
        headers=_headers(client),
    )
    assert resp.status_code == 200, resp.text


def _ready(client) -> None:  # noqa: ANN001
    _register(client)
    _set_ai(client, enabled=True, apiKey="sk-test", baseUrl="http://127.0.0.1:9/v1", model="test-model")


def _new_conversation(client) -> str:  # noqa: ANN001
    resp = client.post("/api/chat/conversations", json={}, headers=_headers(client))
    assert resp.status_code == 200, resp.text
    return resp.json()["conversation"]["id"]


def _send(client, cid: str, **body):  # noqa: ANN001
    return client.post(f"/api/chat/conversations/{cid}/messages", json=body, headers=_headers(client))


def _events(text: str) -> list[tuple[str, dict]]:
    """把 SSE 报文拆成 (事件名, 数据) 列表。"""
    out: list[tuple[str, dict]] = []
    for block in text.strip().split("\n\n"):
        name, data = "", {}
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if name:
            out.append((name, data))
    return out


def _stub(monkeypatch, gen) -> None:  # noqa: ANN001
    monkeypatch.setattr(ai_gateway, "stream_completion", gen)


def _happy_stream():
    def fake(conf, messages, *, tools=None, params=None):
        assert conf["model"] == "test-model", "用的是调用者自己配的模型"
        assert messages[0]["role"] == "system", "历史里要有系统提示"
        yield ("delta", "生产者")
        yield ("delta", "与消费者队列")
        yield ("usage", {"promptTokens": 21, "completionTokens": 6})
        yield ("finish", "stop")

    return fake


def _recording_stream(seen: list) -> object:
    """把上游收到的 messages 记下来，供「上下文从哪儿截」的断言用。"""

    def fake(conf, messages, *, tools=None, params=None):
        seen.append(messages)
        yield ("delta", "好")
        yield ("finish", "stop")

    return fake


def _broken_stream():
    def fake(conf, messages, *, tools=None, params=None):
        yield ("delta", "半句话")
        raise ai_gateway.UpstreamError(
            "接口返回 401", kind="http", status=401, detail="bad key"
        )

    return fake


# ------------------------------------------------------------------ 门禁


def test_chat_never_needs_login(client) -> None:  # noqa: ANN001
    """本地单用户：不带凭据就能列会话（原先这里是断言 401）。"""
    client.cookies.clear()
    assert client.get("/api/chat/conversations").status_code == 200


def test_missing_key_is_rejected_before_streaming(client) -> None:
    """没配密钥时**开流之前**就拦住。

    一旦开始发 SSE，状态码就已经发出去了，那时再报「没填密钥」，
    用户只会看到一个空白的错误 —— 所以这条检查必须在流之前。
    """
    _register(client)
    cid = _new_conversation(client)
    resp = _send(client, cid, content="你好")
    assert resp.status_code == 503
    assert "密钥" in resp.json()["detail"]

    # 也不该留下任何消息（否则用户改好设置回来，会看到一条没有回答的孤零零提问）
    body = client.get(f"/api/chat/conversations/{cid}").json()
    assert body["messages"] == []


# ------------------------------------------------------------------ 主干


def test_stream_persists_the_reply(client, monkeypatch) -> None:  # noqa: ANN001
    _ready(client)
    _stub(monkeypatch, _happy_stream())
    cid = _new_conversation(client)

    resp = _send(client, cid, content="循环缓冲是什么")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")

    events = _events(resp.text)
    names = [name for name, _ in events]
    assert names[0] == "user" and names[1] == "start", "先告诉前端两条消息的 id，再开始吐字"
    assert "delta" in names and names[-1] == "done"

    done = dict(events)["done"]
    assert done["content"] == "生产者与消费者队列", "所有增量拼起来才是正文"
    assert done["status"] == "ok"
    assert done["completionTokens"] == 6, "上游给的用量要落到消息上"

    # 读回来的一份必须与流里那份一致（否则刷新一次会看到不同的内容）
    body = client.get(f"/api/chat/conversations/{cid}").json()
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
    assert body["messages"][1]["content"] == done["content"]

    # 标题由第一条消息生成 —— 用户不该被迫先起个名
    assert body["conversation"]["title"].startswith("循环缓冲是什么")


def test_a_leaked_tool_call_in_the_text_is_dropped(client, monkeypatch) -> None:  # noqa: ANN001
    """模型把工具调用写成正文时：**不发给用户**，只留一条说明。

    实测内测通道的 deepseek-chat 会这样：用户看到的是一屏
    `<||DSML|| invoke name="push_question">` 的原文，而那一轮的工具并没有真的执行
    （它写进了 content，协议里没有 tool_calls）。看着就是"模型在胡说"。
    """

    def fake(conf, messages, *, tools=None, params=None):
        yield ("delta", "我先看看题库里有什么。")
        yield ("delta", '<||DSML|| invoke name="push_question">')
        yield ("delta", '<||DSML|| parameter name="payload">{}</||DSML|| parameter>')
        yield ("finish", "stop")

    _ready(client)
    _stub(monkeypatch, fake)
    cid = _new_conversation(client)

    resp = _send(client, cid, content="出一道题")
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert "DSML" not in body, "泄漏的原文不许发给用户"
    assert "我先看看题库里有什么。" in body, "泄漏之前的正文照常发（不许被一起丢掉）"
    assert "协议泄漏" in body, "要说明这一段被丢了 —— 否则他会以为模型没说话"


def test_upstream_error_keeps_what_was_streamed(client, monkeypatch) -> None:  # noqa: ANN001
    """上游挂了也要把已经吐出来的字留下。"""
    _ready(client)
    _stub(monkeypatch, _broken_stream())
    cid = _new_conversation(client)

    events = _events(_send(client, cid, content="讲讲 mesh").text)
    names = [name for name, _ in events]
    assert names[-1] == "error"
    assert "bad key" in dict(events)["error"]["detail"], "上游原文要带给用户"

    body = client.get(f"/api/chat/conversations/{cid}").json()
    last = body["messages"][-1]
    assert last["status"] == "error"
    assert last["content"] == "半句话", "失败不等于没有产出"


def test_regenerate_adds_a_branch_but_shows_one_line(client, monkeypatch) -> None:  # noqa: ANN001
    """换个说法重问 = 新增分支；界面只画当前那条线。

    如果实现成「把老的删掉重写」，轨迹就没了 —— 而"我上次是怎么被讲明白的"
    正是这套东西最该留住的东西。
    """
    _ready(client)
    _stub(monkeypatch, _happy_stream())
    cid = _new_conversation(client)

    first = dict(_events(_send(client, cid, content="循环缓冲是什么").text))
    user_id = first["user"]["id"]
    first_reply = first["done"]["id"]

    resp2 = _send(client, cid, replyTo=user_id)
    second = dict(_events(resp2.text))
    assert "done" in second, resp2.text
    second_reply = second["done"]["id"]
    assert second_reply != first_reply
    assert second["done"]["parentId"] == user_id, "两个回答挂在同一条提问下"

    body = client.get(f"/api/chat/conversations/{cid}").json()
    assert len(body["messages"]) == 3, "整棵树都给回去（提问 + 两个分支的回答）"
    assert body["activePath"][-1] == second_reply, "默认跟着最新那一支"
    assert sorted(m["id"] for m in body["messages"]) == sorted(
        [user_id, first_reply, second_reply]
    )

    # 但树留在库里：一条提问 + 两个分支上的回答 = 3 条（界面只画 2 条）
    listed = client.get("/api/chat/conversations").json()["conversations"][0]
    assert listed["messageCount"] == 3


def test_new_message_can_hang_off_an_older_branch(client, monkeypatch) -> None:  # noqa: ANN001
    """翻到旧分支上接着问：新消息要挂在那条分支上。

    否则"我切回原来那个答案再追问"会静默变成"接在另一条分支后面" ——
    用户看到的会是莫名其妙的上下文错位。
    """
    _ready(client)
    _stub(monkeypatch, _happy_stream())
    cid = _new_conversation(client)

    first = dict(_events(_send(client, cid, content="第一个问题").text))
    user_id = first["user"]["id"]
    first_reply = first["done"]["id"]

    second = dict(_events(_send(client, cid, replyTo=user_id).text))
    assert second["done"]["parentId"] == user_id, "先造出两个分支"

    third = dict(_events(_send(client, cid, content="接着第一个回答追问", parentId=first_reply).text))
    assert third["user"]["parentId"] == first_reply, "挂在指定的那条分支上"

    body = client.get(f"/api/chat/conversations/{cid}").json()
    # 当前分支：…… → 第一个回答 → 这条追问 → 它的回答
    assert body["activePath"][-2] == third["user"]["id"]
    assert body["activePath"][-1] == third["done"]["id"]


def test_regenerate_does_not_feed_the_old_answer(client, monkeypatch) -> None:  # noqa: ANN001
    """再生成时，上游不该看到上一条回答。

    看到了它就会顺着自己的旧答案往下写 —— 那不是重新回答，是接着写。
    这是实测踩出来的：第一版把「当前分支」当上下文，于是第二次生成
    接在第一次的回答后面（测试里的桩断言直接炸了）。
    """
    _ready(client)
    seen: list[list[dict]] = []
    _stub(monkeypatch, _recording_stream(seen))
    cid = _new_conversation(client)

    events = dict(_events(_send(client, cid, content="循环缓冲是什么").text))
    assert [m["role"] for m in seen[-1]] == ["system", "user"]

    _send(client, cid, replyTo=events["user"]["id"])
    assert [m["role"] for m in seen[-1]] == ["system", "user"], "重问时上下文止于那条提问"
    assert seen[-1][-1]["content"] == "循环缓冲是什么"


def test_editing_the_first_message_keeps_it_at_the_root(client, monkeypatch) -> None:  # noqa: ANN001
    """编辑第一条消息：新那条也得在**根上**，不能因为"没给 parentId"被挪到会话末尾去。

    区分靠的是「parentId 键在不在」而不是「值真不真」—— 这就是这条用例守的东西。
    """
    _ready(client)
    _stub(monkeypatch, _recording_stream([]))
    cid = _new_conversation(client)

    first = dict(_events(_send(client, cid, content="原来的第一问").text))["user"]
    assert first["parentId"] is None

    # 编辑并重发：新的那条挂在 first 的父节点上（也就是根），而不是挂到末尾
    resp = _send(client, cid, content="改过的第一问", parentId=None)
    assert resp.status_code == 200, resp.text
    again = client.get(f"/api/chat/conversations/{cid}").json()
    roots = [m for m in again["messages"] if m["role"] == "user" and not m["parentId"]]
    assert len(roots) == 2, "两条第一问都该在根上（旧那条留着，是树上的一个分支）"
    assert {m["content"] for m in roots} == {"原来的第一问", "改过的第一问"}


def test_regenerate_rejects_a_reply_target(client, monkeypatch) -> None:  # noqa: ANN001
    """只能对**提问**重新生成，不能对回答再回答。"""
    _ready(client)
    _stub(monkeypatch, _happy_stream())
    cid = _new_conversation(client)
    events = dict(_events(_send(client, cid, content="你好").text))
    resp = _send(client, cid, replyTo=events["done"]["id"])
    assert resp.status_code == 400


def test_partial_output_is_in_the_db_before_the_stream_ends(client, monkeypatch, db_session) -> None:  # noqa: ANN001
    """边收边写：用户停下那一刻，屏幕上已经出现的字必须已经在库里。

    这条不是洁癖，是实测踩出来的：只在结束时写库时，客户端一断开，那条消息就永远
    停在 `streaming` 且正文为 0（同步生成器感知不到断开），用户眼睁睁看着的东西白没了。

    这里直接驱动那个生成器（不经过 HTTP），因为要测的正是**中途**：
    拉两块之后 `close()` —— 那就是 Starlette 在客户端断开时做的事。
    """
    from app.models import Conversation, Message
    from app.routers import chat

    _ready(client)
    monkeypatch.setattr(chat, "SPILL_SECONDS", 0)  # 第一条增量就落库

    def two_chunks(conf, messages, *, tools=None, params=None):
        yield ("delta", "第一段")
        yield ("delta", "第二段")
        yield ("finish", "stop")

    _stub(monkeypatch, two_chunks)
    cid = _new_conversation(client)

    conv = db_session.get(Conversation, uuid.UUID(cid))
    user_msg = Message(
        conversation_id=conv.id, role="user", content="问一句", status="ok"
    )
    assistant = Message(
        conversation_id=conv.id,
        parent_id=None,
        role="assistant",
        content="",
        status="streaming",
    )
    db_session.add_all([user_msg, assistant])
    db_session.commit()

    gen = chat._stream(
        db_session,
        conv,
        {"apiKey": "sk-x", "baseUrl": "http://127.0.0.1:9/v1", "model": "m", "timeoutMs": 1000},
        user_msg,
        assistant,
        [{"role": "user", "content": "问一句"}],
    )
    assert next(gen).startswith("event: user")
    assert next(gen).startswith("event: start")
    assert next(gen).startswith("event: delta")
    next(gen)  # 拉第 4 块：这时生成器走到"攒够了，写一次库"

    db_session.expire_all()
    assert db_session.get(Message, assistant.id).content == "第一段", "中途就该已经在库里"

    gen.close()  # = 客户端断开，Starlette 关掉迭代器
    db_session.expire_all()
    row = db_session.get(Message, assistant.id)
    assert row.status == "partial", "断开要收尾成「有产出但被中断」"
    # 收尾写的是"已经收到的全部"：第二块已经发出去了，所以它也在里面
    assert row.content == "第一段第二段"


def test_stop_endpoint_finalizes_and_late_finish_does_not_overwrite(client, db_session) -> None:  # noqa: ANN001
    """「停止」要立刻在库里成为事实，而且事后不能被补完的那一轮改回去。

    服务端感知不到客户端断开，所以由前端点名收尾。若 `_finish` 之后照常覆盖，
    用户下次打开会看到一条自己明明停掉、却被标成完整的回答。
    """
    from app.models import Conversation, Message
    from app.routers import chat

    _ready(client)
    cid = _new_conversation(client)
    conv = db_session.get(Conversation, uuid.UUID(cid))
    assistant = Message(
        conversation_id=conv.id,
        role="assistant",
        content="已经吐出来的半段",
        status="streaming",
    )
    db_session.add(assistant)
    db_session.commit()

    resp = client.post(
        f"/api/chat/conversations/{cid}/messages/{assistant.id}/stop", headers=_headers(client)
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "partial"

    # 迟到的收尾（上游其实还在生成）不该覆盖这次收尾
    db_session.expire_all()
    row = db_session.get(Message, assistant.id)
    chat._finish(
        db_session,
        conv,
        row,
        [parts.text_part("半段"), parts.text_part("后半段")],
        "ok",
        "",
        "stop",
        1234,
        {"promptTokens": 5, "completionTokens": 9},
    )
    db_session.expire_all()
    row = db_session.get(Message, assistant.id)
    assert row.status == "partial", "停止就是停止"
    assert row.content == "已经吐出来的半段"
    assert row.completion_tokens == 9, "但用量要记 —— token 是真花掉了"


def _tool_then_answer():
    """第一轮要工具，第二轮才回答 —— 工具循环的最小形状。"""
    turns: list[list[dict]] = []

    def fake(conf, messages, *, tools=None, params=None):
        turns.append(list(messages))
        if len(turns) == 1:
            assert tools, "第一轮必须带上工具声明"
            yield (
                "tool_calls",
                [
                    {
                        "id": "call_1",
                        "name": "search_knowledge",
                        "arguments": json.dumps({"query": "circular"}),
                    }
                ],
            )
            yield ("finish", "tool_calls")
            return
        # 第二轮：应当看得到上一轮的工具结果
        yield ("delta", "查到了：")
        yield ("delta", "它是核间的队列")
        yield ("usage", {"promptTokens": 10, "completionTokens": 4})
        yield ("finish", "stop")

    return fake, turns


def test_tool_call_shows_up_and_stays_in_the_message(client, monkeypatch) -> None:  # noqa: ANN001
    """调用要留在消息里（零件），也要在流里实时告诉前端。

    只在前端显示、不落库的话，刷新一次那条回答就成了"没来由的一段话"。
    """
    _ready(client)
    fake, turns = _tool_then_answer()
    _stub(monkeypatch, fake)
    cid = _new_conversation(client)

    events = _events(_send(client, cid, content="circular buffer 是什么").text)
    names = [name for name, _ in events]
    assert names.count("tool") == 2, "一次调用 = start + result 两条事件"

    started = next(data for name, data in events if name == "tool" and data["phase"] == "start")
    assert started["name"] == "search_knowledge" and started["args"] == {"query": "circular"}
    finished = next(data for name, data in events if name == "tool" and data["phase"] == "result")
    assert finished["ok"] is True and finished["output"]

    done = dict(events)["done"]
    assert done["content"] == "查到了：它是核间的队列", "正文是各 text 零件拼起来的"
    kinds = [part["type"] for part in done["parts"]]
    assert "tool_call" in kinds and "text" in kinds
    part = next(p for p in done["parts"] if p["type"] == "tool_call")
    assert part["name"] == "search_knowledge" and part["output"]

    assert turns[0][-1]["role"] == "user"
    assert turns[1][-1]["role"] == "tool", "第二轮要带着工具结果"
    assert '"concepts"' in turns[1][-1]["content"], "工具结果真的进了上下文"


def test_the_text_before_a_tool_call_stays_in_the_body(client, monkeypatch) -> None:  # noqa: ANN001
    """工具调用**前面那段话不打折**：它是对用户说的正文，永远留在正文里。

    这里原来有一条"按位置猜"的启发式：紧跟工具调用的 text 打上 `process`、标成
    「过程」，界面上折进折叠块。它猜错过 —— 模型先说一段正文（"我一次验到底：
    两条路各出一张…"）再调工具，那 1500 字被整段折走，正文区只剩工具气泡。
    用户的原话："正文被吞到思考里面了"（还补了一句"ds 的思维链是英文的"：
    该进折叠块的是推理通道，不是中文正文）。

    通道本来就分得清楚：思维链走 `reasoning_content`（think 零件），
    对用户说的话走 `content`（text 零件）。不用我们按位置再猜一次。
    """
    _ready(client)
    turns: list[int] = []

    def fake(conf, messages, *, tools=None, params=None):
        turns.append(1)
        if len(turns) == 1:
            yield ("delta", "我一次验到底：")
            yield ("delta", "两条路各出一张。")
            yield (
                "tool_calls",
                [
                    {
                        "id": "call_1",
                        "name": "search_knowledge",
                        "arguments": json.dumps({"query": "x"}),
                    }
                ],
            )
            yield ("finish", "tool_calls")
            return
        yield ("delta", "两张都出来了。")
        yield ("finish", "stop")

    _stub(monkeypatch, fake)
    cid = _new_conversation(client)
    done = dict(_events(_send(client, cid, content="出两张图").text))["done"]

    texts = [p for p in done["parts"] if p["type"] == "text"]
    assert len(texts) >= 2, "工具前后两段正文都在"
    assert all(not p.get("process") for p in texts), "工具前那段不许被打上 process"
    assert all(not p.get("label") for p in texts), "也不许被标成「过程」"
    assert done["content"] == "我一次验到底：两条路各出一张。\n两张都出来了。", "轮与轮之间用换行接"


def test_the_thought_trail_rides_back_with_the_turn(client, monkeypatch) -> None:  # noqa: ANN001
    """思维链要**回传**：带 tools 时上游要求把 `reasoning_content` 原样带回去，否则 400。

    2026-09-20 实测：`deepseek-flash` 裸调就给思维链（思考模式默认开启、默认
    effort=high），而配置里那个遗留名 `deepseek-chat` 一个字节都不给。所以两条路
    都要在：攒到就回传（不踩 400），攒不到就不带这个字段（请求与从前逐字节一致）。
    """
    _ready(client)
    turns: list[list[dict]] = []

    def fake(conf, messages, *, tools=None, params=None):
        turns.append([{k: v for k, v in m.items()} for m in messages])
        if len(turns) == 1:
            yield ("think", "先想一下：")
            yield ("think", "用户问的是队列。")
            yield ("delta", "我查一下。")
            yield (
                "tool_calls",
                [
                    {
                        "id": "call_1",
                        "name": "search_knowledge",
                        "arguments": json.dumps({"query": "queue"}),
                    }
                ],
            )
            yield ("finish", "tool_calls")
            return
        yield ("delta", "是环形队列。")
        yield ("finish", "stop")

    _stub(monkeypatch, fake)
    cid = _new_conversation(client)
    events = _events(_send(client, cid, content="队列是什么").text)

    thinks = [data["text"] for name, data in events if name == "think"]
    assert thinks == ["先想一下：", "用户问的是队列。"], "思维链原样流给界面"

    later = next(m for m in turns[1] if m.get("role") == "assistant")
    assert later["reasoning_content"] == "先想一下：用户问的是队列。", "攒起来的思维链跟着这一轮回传"
    assert later["content"] == "我查一下。"


def test_thinking_params_are_explicit_and_only_for_known_models() -> None:
    """思考开关只发给"认识的模型"，且两个方向都是**显式**值。

    不依赖上游默认：`deepseek-flash` 裸调就思考、`deepseek-chat` 裸调不思考 ——
    同一颗"开着"的药丸在两个模型上该是同一种行为。名单外的模型一个字节都不加
    （`thinking` 是 DeepSeek 的方言，别家收到不认识的字段可能 400）。
    """
    assert ai_gateway.thinking_params("deepseek-flash", True) == {"thinking": {"type": "enabled"}}
    assert ai_gateway.thinking_params("deepseek-flash", False) == {"thinking": {"type": "disabled"}}
    assert ai_gateway.thinking_params("deepseek-flash-0710", True) == {"thinking": {"type": "enabled"}}
    assert ai_gateway.thinking_params("deepseek-chat", True) == {"thinking": {"type": "enabled"}}
    assert ai_gateway.thinking_params("deepseek-reasoner", False) == {"thinking": {"type": "disabled"}}
    assert ai_gateway.thinking_params("gpt-4o-mini", True) == {}
    assert ai_gateway.thinking_params("", True) == {}


def test_the_deep_think_switch_reaches_the_upstream(client, monkeypatch) -> None:  # noqa: ANN001
    """「深度思考」药丸：缺省 = 开；关掉就是给上游一个显式的 `disabled`。"""
    _register(client)
    _set_ai(
        client, enabled=True, apiKey="sk-test", baseUrl="http://127.0.0.1:9/v1", model="deepseek-flash"
    )
    seen: list[dict] = []

    def fake(conf, messages, *, tools=None, params=None):
        seen.append(dict(params or {}))
        yield ("delta", "好")
        yield ("finish", "stop")

    _stub(monkeypatch, fake)
    cid = _new_conversation(client)
    _events(_send(client, cid, content="默认这条").text)
    _events(_send(client, cid, content="关掉思考这条", thinking=False).text)

    assert seen[0] == {"thinking": {"type": "enabled"}}, "缺省 = 开（药丸默认开着）"
    assert seen[1] == {"thinking": {"type": "disabled"}}, "关掉就给显式的 disabled"


def test_the_request_body_carries_thinking_to_a_real_endpoint(client) -> None:  # noqa: ANN001
    """钉住**真正发出去的那份 body**：本地起一个假上游，看它收到什么。

    前面那条测试钉的是"传给 gateway 的 params"；这一条钉的是"最后拼进 HTTP
    请求体的东西" —— 中间还隔着 `stream_completion` 的 body 构造，断在那儿
    是最难发现的一种（界面上一切正常，只是模型不思考）。
    """
    import http.server
    import json as _json
    import threading

    seen: dict = {}

    class _Upstream(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            size = int(self.headers.get("content-length") or 0)
            try:
                seen.update(_json.loads(self.rfile.read(size) or b"{}"))
            except ValueError:
                seen["raw"] = "无法解析"
            payload = (
                'data: {"choices":[{"delta":{"reasoning_content":"想一下"}}]}\n\n'
                'data: {"choices":[{"delta":{"content":"好"}}]}\n\n'
                "data: [DONE]\n\n"
            )
            raw = payload.encode("utf-8")
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("content-length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args) -> None:  # noqa: ANN002
            """别把访问日志混进测试输出。"""

    server = http.server.HTTPServer(("127.0.0.1", 0), _Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        _register(client)
        _set_ai(
            client,
            enabled=True,
            apiKey="sk-test",
            baseUrl="http://127.0.0.1:%d/v1" % server.server_port,
            model="deepseek-flash",
        )
        cid = _new_conversation(client)
        events = _events(_send(client, cid, content="你好").text)

        assert seen.get("thinking") == {"type": "enabled"}, "body 里少了思考开关：%r" % (
            sorted(seen),
        )
        thinks = [data["text"] for name, data in events if name == "think"]
        assert thinks == ["想一下"], "上游给的思维链要流到前端"
    finally:
        server.shutdown()


def test_models_without_tool_support_degrade_instead_of_erroring(client, monkeypatch) -> None:  # noqa: ANN001
    """不支持工具调用的模型：摘掉工具重来一次，而不是把 400 甩给用户。

    与设置面板里 `response_format` 的降级是同一条思路 —— 供应商做不到，
    就退回我们能做到的那一档。
    """
    seen: list[object] = []

    def picky(conf, messages, *, tools=None, params=None):
        seen.append(tools)
        if tools:
            raise ai_gateway.UpstreamError(
                "接口返回 400",
                kind="http",
                status=400,
                detail='{"error":{"message":"unknown parameter: tools"}}',
            )
        yield ("delta", "没有工具也能答")
        yield ("finish", "stop")

    _ready(client)
    _stub(monkeypatch, picky)
    cid = _new_conversation(client)

    events = _events(_send(client, cid, content="试试").text)
    names = [name for name, _ in events]
    assert "note" in names and "error" not in names
    note = next(data for name, data in events if name == "note")
    assert "不支持工具" in note["text"]
    assert dict(events)["done"]["content"] == "没有工具也能答"
    assert seen[0] and seen[-1] is None, "第二次要真的不带工具"


def test_tool_loop_stops_at_the_limit(client, monkeypatch) -> None:  # noqa: ANN001
    """模型反复要工具时必须停下 —— 那是在烧额度，不是在干活。"""
    def always(conf, messages, *, tools=None, params=None):
        yield ("tool_calls", [{"id": "c", "name": "get_mastery", "arguments": "{}"}])
        yield ("finish", "tool_calls")

    _ready(client)
    _stub(monkeypatch, always)
    cid = _new_conversation(client)

    events = _events(_send(client, cid, content="来").text)
    names = [name for name, _ in events]
    assert "error" in names and "done" not in names
    error = next(data for name, data in events if name == "error")
    assert "上限" in error["message"] and error["retryable"] is False


def test_search_finds_messages_across_conversations(client, monkeypatch) -> None:  # noqa: ANN001
    """「我到底在哪儿学过这个」：跨会话搜，带一段能看清的上下文。"""
    _ready(client)
    _stub(monkeypatch, _happy_stream())

    first = _new_conversation(client)
    _send(client, first, content="循环缓冲是什么")
    second = _new_conversation(client)
    _send(client, second, content="再聊点别的")

    body = client.get("/api/chat/search", params={"q": "循环缓冲"}).json()
    assert body["query"] == "循环缓冲"
    assert body["items"], body
    hit = body["items"][0]
    assert hit["conversationId"] == first, "命中的是第一个会话"
    assert "循环缓冲" in hit["snippet"], "摘要里要能看到命中的那几个字"
    assert hit["title"], "要带上会话标题，否则不知道是哪一条线"


def test_search_needs_at_least_two_characters(client, monkeypatch) -> None:  # noqa: ANN001
    """一个字会命中几乎所有消息 —— 那不是搜索。"""
    _ready(client)
    _stub(monkeypatch, _happy_stream())
    cid = _new_conversation(client)
    _send(client, cid, content="循环缓冲是什么")

    body = client.get("/api/chat/search", params={"q": "的"}).json()
    assert body["items"] == [] and "两个字" in body["note"]


def test_the_last_turn_answers_without_tools(client, monkeypatch, imported_bank) -> None:  # noqa: ANN001
    """工具预算用尽时就该**收口作答**。

    实测症状：问"该先补什么"，模型把六轮全花在检索上，用户最后只拿到一堆
    工具气泡和一句"我先把这些核一遍"—— 一个字结论都没有。所以最后一轮
    不再给它工具：它手里已经有查到的东西了，该把它说出来。
    """
    offered: list = []

    def fake(conf, messages, *, tools=None, params=None):
        offered.append(tools)
        if tools:
            yield (
                "tool_calls",
                [{"id": "c" + str(len(offered)), "name": "get_mastery", "arguments": "{}"}],
            )
            yield ("finish", "tool_calls")
            return
        yield ("delta", "结论：先补张量轴约定，再看切分策略。")
        yield ("finish", "stop")

    _ready(client)
    _stub(monkeypatch, fake)
    cid = _new_conversation(client)
    events = _events(_send(client, cid, content="我该先补什么？").text)

    assert offered[:-1] and all(item is not None for item in offered[:-1]), "前面几轮照常给工具"
    assert offered[-1] is None, "最后一轮不给工具"

    names = [name for name, _ in events]
    assert "error" not in names, "不该以「工具调用达到上限」收场"

    done = dict(events)["done"]
    texts = [part.get("text", "") for part in done["parts"] if part["type"] == "text"]
    assert any("结论" in text for text in texts), texts


def test_sources_become_citations(client, monkeypatch, db_session, tmp_path) -> None:  # noqa: ANN001
    """"有没有引用"不取决于哪个工具，而取决于它有没有给出处。

    任何工具只要在结果里带 `sources`，这一处就统一把它变成可点回原文的引用。
    所以"贴材料问"与"查概念"两条路，引用长得一模一样。
    同一条引用在一条消息里只挂一次（两个工具都碰上同一段，不该出现两遍）。
    """
    body = ["前一行", "circular buffer 是核间通信的队列", "后一行"]
    path = tmp_path / "cite.md"
    path.write_text("\n".join(body), encoding="utf-8")
    slug = "tt-metal-cite-" + uuid.uuid4().hex[:6]
    db_session.add(
        Material(
            slug=slug,
            subject="tt-metal",
            title="引用测试材料",
            source_path=str(path),
            sha256="0" * 64,
            lines=3,
            # `search_material` 只看"出题"这一档（另一档归 `search_library`）；
            # 不写这句就是模型的默认值 `检索`，那个工具一条都搜不到。
            depth="出题",
        )
    )
    db_session.commit()

    call = {
        "id": "c1",
        "name": "search_material",
        "arguments": json.dumps({"query": "circular buffer", "slug": slug}),
    }

    def fake(conf, messages, *, tools=None, params=None):
        if not any(message.get("role") == "tool" for message in messages):
            # 同一条引用问两次：去重该挡住第二个
            yield (
                "tool_calls",
                [call, dict(call, id="c2")],
            )
            yield ("finish", "tool_calls")
            return
        yield ("delta", "材料里是这么说的。")
        yield ("finish", "stop")

    _ready(client)
    _stub(monkeypatch, fake)
    cid = _new_conversation(client)

    events = _events(_send(client, cid, content="材料里怎么说 circular buffer？").text)
    citations = [data for name, data in events if name == "citation"]
    assert len(citations) == 1, "同一段材料在一条消息里只该挂一次"

    citation = citations[0]["citation"]
    assert citation["slug"] == slug
    assert citation["quote"] == body[1]
    assert citation["startLine"] >= 1 and citation["endLine"] >= citation["startLine"]

    done = dict(events)["done"]
    part = next(p for p in done["parts"] if p["type"] == "citation")
    assert part["slug"] == slug and part["quote"] == body[1]


def test_a_write_tool_becomes_a_pending_receipt(client, monkeypatch, imported_bank) -> None:  # noqa: ANN001
    """写操作不直接生效：消息里长出一张**待确认的凭条**，点了才落到记录里。

    这条路刻意和题卡不一样：题卡是"问他一个问题"，凭条是"动他的东西"。
    后者必须他点头 —— 所以工具只提案，界面上出现可点的控件。
    """
    def fake(conf, messages, *, tools=None, params=None):
        if not any(message.get("role") == "tool" for message in messages):
            yield (
                "tool_calls",
                [
                    {
                        "id": "c1",
                        "name": "flag_question",
                        "arguments": json.dumps({"questionId": question_id}),
                    }
                ],
            )
            yield ("finish", "tool_calls")
            return
        yield ("delta", "要不要把这题收进收藏夹？")
        yield ("finish", "stop")

    _ready(client)
    question_id = client.get("/api/bank").json()["questions"][0]["id"]
    _stub(monkeypatch, fake)
    cid = _new_conversation(client)

    events = _events(_send(client, cid, content="这题留着").text)
    assert "action" in [name for name, _ in events], events

    proposal = next(data for name, data in events if name == "action")["proposal"]
    assert proposal["questionId"] == question_id
    assert proposal["kind"] == "flag" and proposal["current"] is False

    done = dict(events)["done"]
    part = next(p for p in done["parts"] if p["type"] == "action")
    assert part["kind"] == "flag" and part["payload"]["questionId"] == question_id

    # 凭条本身没写任何东西：记录里还是干干净净
    records = client.get("/api/progress").json().get("records") or {}
    assert question_id not in records, "提案阶段不该动记录"


def test_history_replays_previous_tool_calls(client, monkeypatch) -> None:  # noqa: ANN001
    """第二轮的上下文里要看得见上一轮的工具调用与结果。

    只有文本的历史会让模型以为"交代一句就够了" —— 它照着自己那份残缺的
    transcript 学，于是"考我一道"有一半次数只得到一句承诺、什么都没做。
    """
    seen: list[list[dict]] = []

    def fake(conf, messages, *, tools=None, params=None):
        seen.append([dict(message) for message in messages])
        if not any(message.get("role") == "tool" for message in messages):
            yield (
                "tool_calls",
                [{"id": "c9", "name": "get_mastery", "arguments": '{"pointKeys": []}'}],
            )
            yield ("finish", "tool_calls")
            return
        yield ("delta", "好")
        yield ("finish", "stop")

    _ready(client)
    _stub(monkeypatch, fake)
    cid = _new_conversation(client)
    _send(client, cid, content="第一问")
    _send(client, cid, content="第二问")

    history = seen[-1]
    roles = [message["role"] for message in history]
    assert "tool" in roles, roles

    assistant = next(m for m in history if m["role"] == "assistant" and m.get("tool_calls"))
    assert assistant["tool_calls"][0]["id"] == "c9"
    assert assistant["tool_calls"][0]["function"]["name"] == "get_mastery"
    assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {"pointKeys": []}

    result = next(m for m in history if m["role"] == "tool")
    assert result["tool_call_id"] == "c9" and result["content"]


def test_push_question_becomes_a_card_in_the_message(client, monkeypatch, imported_bank) -> None:  # noqa: ANN001
    """题卡：模型调一次工具，消息里就"长出"一道可作答的题。

    这是对话第一次不只是文字 —— 卡是消息的零件，所以刷新之后它还在。
    """
    def fake(conf, messages, *, tools=None, params=None):
        if not any(m.get("role") == "tool" for m in messages):
            yield ("tool_calls", [{"id": "c1", "name": "push_question", "arguments": "{}"}])
            yield ("finish", "tool_calls")
            return
        yield ("delta", "先做这道，做完我们再看。")
        yield ("finish", "stop")

    _ready(client)
    _stub(monkeypatch, fake)
    cid = _new_conversation(client)

    events = _events(_send(client, cid, content="考考我").text)
    names = [name for name, _ in events]
    assert "card" in names, names

    card = next(data for name, data in events if name == "card")["card"]
    assert card["questionId"] and card["stem"]
    assert "answer" not in card and "rubric" not in card, "卡里不许有答案"

    done = dict(events)["done"]
    kinds = [part["type"] for part in done["parts"]]
    assert "card" in kinds and "tool_call" in kinds and "text" in kinds
    part = next(p for p in done["parts"] if p["type"] == "card")
    assert part["kind"] == "question"
    assert part["payload"]["questionId"] == card["questionId"]

    # 再读一次会话：卡片是从库里读回来的，不是只闪在前端
    again = client.get(f"/api/chat/conversations/{cid}").json()
    last = again["messages"][-1]
    assert any(p["type"] == "card" for p in last["parts"])


# ------------------------------------------------------------------ 置顶


def test_pinned_conversations_come_first(client, monkeypatch) -> None:  # noqa: ANN001
    """置顶压过"谁最近动过" —— 它是用户自己钉的。

    顺带守住 `pinned: false`：那个值**不能**被当成"没给"
    （写成 `body.get("pinned") or conv.pinned` 就会，取消置顶永远无效）。
    """
    _ready(client)
    _stub(monkeypatch, _recording_stream([]))
    first = _new_conversation(client)
    _send(client, first, content="第一条")
    second = _new_conversation(client)
    _send(client, second, content="第二条")

    listed = client.get("/api/chat/conversations").json()["conversations"]
    assert listed[0]["id"] == second, "默认按最近活动"

    resp = client.patch(
        f"/api/chat/conversations/{first}", json={"pinned": True}, headers=_headers(client)
    )
    assert resp.status_code == 200 and resp.json()["pinned"] is True

    listed = client.get("/api/chat/conversations").json()["conversations"]
    assert listed[0]["id"] == first and listed[0]["pinned"] is True

    client.patch(
        f"/api/chat/conversations/{first}", json={"pinned": False}, headers=_headers(client)
    )
    listed = client.get("/api/chat/conversations").json()["conversations"]
    assert listed[0]["id"] == second
    assert all(c["pinned"] is False for c in listed), "取消置顶要真的生效"


# ------------------------------------------------------------------ 附件


def test_an_attachment_rides_along_into_the_request(client, monkeypatch) -> None:  # noqa: ANN001
    """附件：先上传、再引用。

    两条边界都要守住：抽出来的正文**进发给模型的那一份**，而库里那条用户消息的
    `content` 仍是**他的原话** —— 把附件正文写进去，等于伪造他说过的话。
    """
    seen: list[list[dict]] = []

    def fake(conf, messages, *, tools=None, params=None):
        seen.append([dict(message) for message in messages])
        yield ("delta", "看到了")
        yield ("finish", "stop")

    _ready(client)
    _stub(monkeypatch, fake)
    cid = _new_conversation(client)

    up = client.post(
        "/api/chat/attachments",
        files={"file": ("note.md", "# 环形缓冲\n生产者写、消费者读。".encode(), "text/markdown")},
        headers=_headers(client),
    )
    assert up.status_code == 200, up.text
    info = up.json()
    assert info["kind"] == "text" and info["textChars"] > 0
    assert "生产者写" in info["preview"]

    resp = _send(client, cid, content="这份笔记讲了什么", attachments=[info["id"]])
    assert resp.status_code == 200, resp.text
    events = dict(_events(resp.text))

    # 附件零件挂在**用户消息**上（不是助手那条）
    part = next(p for p in events["user"]["parts"] if p["type"] == "file")
    assert part["name"] == "note.md" and part["attachmentId"] == info["id"]
    assert part["size"] > 0

    user_text = [m["content"] for m in seen[-1] if m["role"] == "user"][-1]
    assert "生产者写、消费者读" in user_text, "抽出来的正文要进上下文"
    assert "【附件：note.md" in user_text

    stored = [m for m in client.get(f"/api/chat/conversations/{cid}").json()["messages"] if m["role"] == "user"][-1]
    assert stored["content"] == "这份笔记讲了什么", "库里那条是用户原话"
    assert stored["parts"][0]["type"] == "file"


def test_the_sandbox_output_comes_back_by_itself(client, monkeypatch) -> None:  # noqa: ANN001
    """跑完的输出**自动回填**：消息里带着它，下一轮模型也读得到。

    原先靠面板上一个「把输出发给它」按钮 —— 那是让用户当传话筒。
    """
    seen: list[list[dict]] = []

    def fake(conf, messages, *, tools=None, params=None):
        seen.append([dict(message) for message in messages])
        if not any(message.get("role") == "tool" for message in messages):
            yield (
                "tool_calls",
                [
                    {
                        "id": "c1",
                        "name": "run_python",
                        "arguments": json.dumps({"code": "print(1 + 1)"}),
                    }
                ],
            )
            yield ("finish", "tool_calls")
            return
        yield ("delta", "跑好了。")
        yield ("finish", "stop")

    _ready(client)
    _stub(monkeypatch, fake)
    cid = _new_conversation(client)
    events = dict(_events(_send(client, cid, content="算个 1+1").text))

    done = events["done"]
    demo = next((part for part in done["parts"] if part["type"] == "demo"), None)
    if demo is None:
        # 这条依赖**本机 Docker 沙箱**：沙箱没起来时 run_python 跑了但没有产出，
        # 于是这里原本抛一个看不懂的 StopIteration（全量跑偶发、单跑通常过）。
        # 但要分清两种情况 —— 连工具都没调用，那是真回归，必须响。
        called = "run_python" in json.dumps(done.get("parts") or [], ensure_ascii=False)
        if not called:
            pytest.fail("连 run_python 都没调用，这不是沙箱环境的问题")
        pytest.skip("沙箱没起来（需要本机 Docker），零件里不会产出 demo")
    run_id = demo.get("runId")
    assert run_id, "零件要带上 runId，宿主才认得出是哪次运行"

    # 认错 runId 时不该悄悄写坏零件
    bad = client.post(
        f"/api/chat/conversations/{cid}/messages/{done['id']}/run",
        json={"runId": "nope", "text": "x"},
        headers=_headers(client),
    )
    assert bad.status_code == 404

    # 前端回填（沙箱跑完 postMessage → POST 到这里）。带一张图 ——
    # 壳从 matplotlib 收走的那种（base64），落进零件后刷新还在。
    tiny_png = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    ok = client.post(
        f"/api/chat/conversations/{cid}/messages/{done['id']}/run",
        # 上报的 `text` 就是脚本自己的输出（壳的内部账不进这里 —— 见 tools.py 的常驻壳）
        json={"runId": run_id, "ok": True, "text": "numpy 1.26.4\n结果是 2\n", "images": [tiny_png]},
        headers=_headers(client),
    )
    assert ok.status_code == 200, ok.text

    # 存下来了 —— 刷新之后界面上还看得到
    stored = next(
        m for m in client.get(f"/api/chat/conversations/{cid}").json()["messages"] if m["id"] == done["id"]
    )
    part = next(p for p in stored["parts"] if p["type"] == "demo")
    assert part["run"]["text"].startswith("numpy 1.26.4")
    assert part["run"]["ok"] is True
    assert part["run"]["images"] == [tiny_png], "图要跟着运行一起存下来（刷新后还在）"

    # 下一轮：它进模型的历史（模型自己就能读到，不必谁转述）
    _send(client, cid, content="那结果是多少")
    assistant = "\n".join(
        str(m.get("content") or "") for m in seen[-1] if m.get("role") == "assistant"
    )
    assert "〔沙箱输出〕" in assistant
    assert "结果是 2" in assistant


def test_vision_is_detected_conservatively() -> None:
    """认不出就当读不了 —— 猜"能读"会给上游塞 image_url，上游直接 400。"""
    from app import ai_gateway as gateway

    assert gateway.model_reads_images("gpt-4o") is True
    assert gateway.model_reads_images("qwen2.5-vl-72b-instruct") is True
    assert gateway.model_reads_images("glm-4v-plus") is True
    # 实测：deepseek-chat 能读图（名字里的 chat 不代表纯文本）。这条断言是
    # "模型名推不出能力"这件事的钉子 —— 改动它之前请先真的发一张图试试。
    assert gateway.model_reads_images("deepseek-chat") is True
    assert gateway.model_reads_images("my-local-llm") is False
    assert gateway.model_reads_images("") is False


def test_images_reach_a_vision_model(client, monkeypatch) -> None:  # noqa: ANN001
    """能读图的模型：图片作为**图像**发出去，而不是只丢一个文件名。

    这是"用户看到的图"与"模型看到的图"之间唯一的桥。文本模型那条路
    永远只能看到一行文件名（所以它会说读不到，见下一条用例）。
    """
    seen: list[list[dict]] = []

    def fake(conf, messages, *, tools=None, params=None):
        seen.append([dict(message) for message in messages])
        yield ("delta", "看到了")
        yield ("finish", "stop")

    # 一次写清：设置是整体覆盖 + 按 rev 的 LWW，写两次同 rev 的第二次会被丢掉
    _register(client)
    _set_ai(
        client,
        enabled=True,
        apiKey="sk-test",
        baseUrl="http://127.0.0.1:9/v1",
        model="test-model",
        vision=True,  # 显式声明"这个模型能读图"（自动判断按名字来，test-model 认不出）
    )
    _stub(monkeypatch, fake)
    cid = _new_conversation(client)

    # 这一层不解析图片，只搬运字节 —— 头对了就够
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    up = client.post(
        "/api/chat/attachments",
        files={"file": ("shot.png", png, "image/png")},
        headers=_headers(client),
    )
    assert up.status_code == 200, up.text
    info = up.json()
    assert info["kind"] == "image" and info["textChars"] == 0

    resp = _send(client, cid, content="这张图里有什么", attachments=[info["id"]])
    assert resp.status_code == 200, resp.text

    user = [m for m in seen[-1] if m["role"] == "user"][-1]
    content = user["content"]
    assert isinstance(content, list), "能读图的模型收到的应是文本块 + 图像块"
    assert content[0]["type"] == "text"
    assert "这张图里有什么" in content[0]["text"]
    assert "图像已随这条消息交给你" in content[0]["text"]

    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_a_text_model_is_told_why_it_cannot_see_the_image(client, monkeypatch) -> None:  # noqa: ANN001
    """读不了图的模型：说明里要写清**为什么**、以及能怎么办。

    原先只说"读不到"，用户看到的是一句无解的话（实测他就是这么问回来的）。
    """
    seen: list[list[dict]] = []

    def fake(conf, messages, *, tools=None, params=None):
        seen.append([dict(message) for message in messages])
        yield ("delta", "嗯")
        yield ("finish", "stop")

    _ready(client)  # test-model、不带 vision → 自动判断为"读不了"
    _stub(monkeypatch, fake)
    cid = _new_conversation(client)

    up = client.post(
        "/api/chat/attachments",
        files={"file": ("shot.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 64, "image/png")},
        headers=_headers(client),
    )
    info = up.json()
    resp = _send(client, cid, content="这张图里有什么", attachments=[info["id"]])
    assert resp.status_code == 200, resp.text

    user = [m for m in seen[-1] if m["role"] == "user"][-1]
    assert isinstance(user["content"], str), "读不了图时不发图像块"
    text = user["content"]
    assert "【附件：shot.png" in text
    assert "test-model" in text, "要说清是哪个模型读不了"
    assert "读不到图像内容" in text
    assert "换一个能读图的模型" in text, "光说读不到没有用，得给出路"


def test_an_unsupported_image_format_is_not_sent_but_is_explained(client, monkeypatch) -> None:  # noqa: ANN001
    """上游只收 png / jpeg / webp / gif。

    bmp、svg 存得下（`attachments.IMAGE_SUFFIXES` 比上游宽），但传上去会 400 ——
    所以不发；**但不能静默丢掉**：说明里要讲清是哪张、为什么没给出去，
    否则模型会以为"用户没发图"。
    """
    seen: list[list[dict]] = []

    def fake(conf, messages, *, tools=None, params=None):
        seen.append([dict(message) for message in messages])
        yield ("delta", "嗯")
        yield ("finish", "stop")

    _register(client)
    _set_ai(
        client,
        enabled=True,
        apiKey="sk-test",
        baseUrl="http://127.0.0.1:9/v1",
        model="test-model",
        vision=True,
    )
    _stub(monkeypatch, fake)
    cid = _new_conversation(client)

    up = client.post(
        "/api/chat/attachments",
        files={"file": ("shot.bmp", b"BM" + b"\x00" * 64, "image/bmp")},
        headers=_headers(client),
    )
    info = up.json()
    assert info["kind"] == "image"

    resp = _send(client, cid, content="这张图里有什么", attachments=[info["id"]])
    assert resp.status_code == 200, resp.text

    user = [m for m in seen[-1] if m["role"] == "user"][-1]
    assert isinstance(user["content"], str), "格式不收就不该发图像块"
    assert "image/bmp" in user["content"]
    assert "转成 png 或 jpg" in user["content"]


def test_an_attachment_can_be_read_back(client, monkeypatch) -> None:  # noqa: ANN001
    """附件上传后读得回来。

    （原先这里测的是"别人的附件按 404"。单用户本地形态下没有"别人" ——
      见 `app/deps.py`；`attachments.user_id` 那一列仍在，只是恒为同一个值。）
    """
    _ready(client)
    up = client.post(
        "/api/chat/attachments",
        files={"file": ("mine.txt", b"hello", "text/plain")},
        headers=_headers(client),
    )
    aid = up.json()["id"]
    assert client.get(f"/api/chat/attachments/{aid}").status_code == 200


# ------------------------------------------------------------------ 导出


def test_export_gives_markdown_for_reading_and_json_for_the_tree(client, monkeypatch) -> None:  # noqa: ANN001
    """md 只画当前分支（拿去读、归档）；json 带走整棵树（拿去备份）。"""
    _ready(client)
    _stub(monkeypatch, _recording_stream([]))
    cid = _new_conversation(client)
    _send(client, cid, content="第一问")

    md = client.get(f"/api/chat/conversations/{cid}/export?format=md")
    assert md.status_code == 200
    assert "## 我" in md.text and "第一问" in md.text
    assert "attachment" in md.headers.get("content-disposition", "")

    payload = client.get(f"/api/chat/conversations/{cid}/export?format=json").json()
    assert payload["format"] == "quizforge-chat/1"
    assert payload["messages"] and payload["messages"][0]["parentId"] is None

    every = client.get("/api/chat/export").json()
    assert any(c["id"] == cid for c in every["conversations"])


# ------------------------------------------------------------------ 演示沙箱


def test_render_demo_becomes_a_sandbox_part(client, monkeypatch) -> None:  # noqa: ANN001
    """演示沙箱：模型给一段自包含 HTML，消息里长出一个 demo 零件（前端塞进 sandbox iframe）。"""
    html = "<html><body><canvas id='c'></canvas><script>1</script></body></html>"

    def fake(conf, messages, *, tools=None, params=None):
        if not any(message.get("role") == "tool" for message in messages):
            yield (
                "tool_calls",
                [
                    {
                        "id": "c1",
                        "name": "render_demo",
                        "arguments": json.dumps({"title": "数据流", "html": html}),
                    }
                ],
            )
            yield ("finish", "tool_calls")
            return
        yield ("delta", "看这个演示。")
        yield ("finish", "stop")

    _ready(client)
    _stub(monkeypatch, fake)
    cid = _new_conversation(client)

    events = _events(_send(client, cid, content="画个图讲数据怎么流").text)
    assert "demo" in [name for name, _ in events], events

    demo = next(data for name, data in events if name == "demo")["demo"]
    assert demo["title"] == "数据流"
    # 页面里带着模型写的东西，以及服务端注入的套件（它自己不必引库）
    assert "<canvas id='c'>" in demo["html"] and "<script>1</script>" in demo["html"]
    assert "__ORIGIN__/assets/demo-kit/react.js" in demo["html"]

    part = next(p for p in dict(events)["done"]["parts"] if p["type"] == "demo")
    assert part["title"] == "数据流" and "<canvas" in part["html"]


# ------------------------------------------------------------------ 消息序列


def _assert_no_orphan_tool(messages: list[dict]) -> None:
    """上游的硬要求：每条 `tool` 必须紧跟在它对应的 `tool_calls` 后面。"""
    active: set = set()
    for message in messages[1:]:  # 跳过 system
        if message.get("role") == "tool":
            assert message["tool_call_id"] in active, "孤立 tool 消息：" + str(message)[:100]
        if message.get("tool_calls"):
            active = {call["id"] for call in message["tool_calls"]}


def test_truncation_never_splits_a_tool_call_from_its_result() -> None:
    """按条截断会正好落在 assistant(tool_calls) 与它的 tool 回复中间 ——
    于是请求的第一条成了 `role: tool`，上游直接 400：

        Messages with role 'tool' must be a response to a preceding message with 'tool_calls'

    实测撞过：一条真对话在冒出「更早的 3 条消息没有带进来」之后就一直失败。
    截断的单位必须是**组**，不是条。
    """
    history = [
        {"role": "user", "content": "第一问"},   # 会被**钉住**（见下面那条用例）
        # 中间这条是拿来被丢的：开头钉住之后，这一条才是"预算下第一个出局的"。
        # 没有它，这个预算下三条全进得去，`dropped > 0` 就不成立、这条用例就空转了。
        {"role": "user", "content": "中间那一问" + "水" * 400},
        {
            "role": "assistant",
            "content": "查" * 400,  # 故意比它的 tool 回复长：预算正好卡在两者之间
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "get_mastery", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "结果"},
    ]

    messages, dropped = agent_loop.build_messages("系统", history, budget=200)
    assert dropped > 0, "这个预算下必须真的截断了，否则这条用例什么都没测到"
    _assert_no_orphan_tool(messages)

    messages, dropped = agent_loop.build_messages("系统", history, budget=100000)
    assert dropped == 0
    _assert_no_orphan_tool(messages)


def test_the_opening_of_the_path_is_pinned_when_the_budget_is_tight() -> None:
    """预算不够时，**开头那一段不许丢** —— 它是"我在哪"的第一半。

    这条链是"根 → 他正在问的那一条"，所以回溯到根那一段时模型一定看得见最早在干什么；
    但"从最新往前塞"的兜底会**第一个丢掉根** —— 而 `docs/对话树.md` §七 把这一端写死了：

        **当前路径（根 → 我现在在哪）：全留**（这是"我在哪"，丢了就断线）

    实测那条 45 层、46 条的链全带上 5.3 万 tokens，1M 预算下一条不丢；这条用例守的是
    预算不够时（换小窗口模型、或自己把 `maxContextTokens` 调低）的兜底。
    """
    history = [{"role": "user", "content": "最早那一问：我准备学 WCET"}] + [
        {"role": "user", "content": "中间第 %d 问" % i + "啰嗦" * 200} for i in range(1, 12)
    ] + [{"role": "assistant", "content": "最后那条回答"}]

    # 预算只够"开头 + 最近那一条"：中间那十几问都要出局
    messages, dropped = agent_loop.build_messages("系统", history, budget=150)
    kept = [one for one in messages if one.get("role") != "system"]

    assert dropped > 0, "这个预算下必须真的截断了，否则这条用例什么都没测到"
    assert kept[0]["content"].startswith("最早那一问"), "根那一段被丢掉了 —— 那就断线了"
    assert kept[-1]["content"] == "最后那条回答", "最近的那一条也要在"
    assert all("中间第" not in one["content"] for one in kept), "丢的是**中间**，不是两头"


def test_a_broken_message_sequence_is_not_read_as_unsupported_tools() -> None:
    """那句报错里含 `tool_calls`，但它说的是**消息序列不合法**，
    不是「不认识 tools 这个参数」。判错方向的代价：摘掉工具重试
    （拿同样坏的历史再撞一次），还把用户引去查模型。
    """
    broken = ai_gateway.UpstreamError(
        "接口返回 400",
        kind="http",
        status=400,
        detail=(
            '{"error":{"message":"Messages with role \'tool\' must be a response '
            "to a preceding message with 'tool_calls'\"}}"
        ),
    )
    assert agent_loop._tools_unsupported(broken) is False

    really = ai_gateway.UpstreamError(
        "接口返回 400",
        kind="http",
        status=400,
        detail='{"error":{"message":"unknown parameter: tools"}}',
    )
    assert agent_loop._tools_unsupported(really) is True


# 这里原先还有一节「归属」：另一个账号拿别人的会话应当 404 而不是 403。
# 单用户本地形态下没有"别人的会话"（见 `app/deps.py`），这一节随之删掉。


def test_rename_and_delete(client, monkeypatch) -> None:  # noqa: ANN001
    _ready(client)
    _stub(monkeypatch, _happy_stream())
    cid = _new_conversation(client)
    _send(client, cid, content="你好")

    assert client.patch(f"/api/chat/conversations/{cid}", json={"title": "网络编程"}, headers=_headers(client)).status_code == 200
    assert client.get("/api/chat/conversations").json()["conversations"][0]["title"] == "网络编程"

    assert client.patch(f"/api/chat/conversations/{cid}", json={"title": "   "}, headers=_headers(client)).status_code == 400
    assert client.delete(f"/api/chat/conversations/{cid}", headers=_headers(client)).status_code == 200
    assert client.get(f"/api/chat/conversations/{cid}").status_code == 404


# ------------------------------------------------------------------ 自愈


def test_stale_streaming_is_healed_on_read(client, db_session) -> None:  # noqa: ANN001
    """「流到一半没人收尾」的消息，读的时候顺手治一下。

    进程被杀、连接被中间层掐断时，库里会留下一条永远的 streaming ——
    界面会一直转圈。这条规则让那种行自己变成「已中断」，产出仍在。
    """
    from app.models import Conversation, Message

    _ready(client)
    cid = _new_conversation(client)

    conv = db_session.get(Conversation, uuid.UUID(cid))
    db_session.add(
        Message(
            conversation_id=conv.id,
                role="assistant",
            content="收了一半",
            status="streaming",
            created_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        )
    )
    db_session.commit()

    body = client.get(f"/api/chat/conversations/{cid}").json()
    assert body["messages"][-1]["status"] == "partial"

    # 还在生成中的（刚建的）不能被误判
    db_session.add(
        Message(
            conversation_id=conv.id,
                parent_id=body["messages"][-1]["id"],
            role="assistant",
            content="",
            status="streaming",
        )
    )
    db_session.commit()
    body = client.get(f"/api/chat/conversations/{cid}").json()
    assert body["messages"][-2]["status"] == "partial"
    assert body["messages"][-1]["status"] == "streaming", "刚开的那条要留着继续收"


# ------------------------------------------------------------------ 分组
#
# 用户的诉求原话是"让对话也能像文件一样可以放文件夹里"。这里钉住四件事：
# 分组能建、会话能挪进去、改名时整棵子树跟着走、以及**里面还有东西不许删**。


def _folders(client) -> dict:  # noqa: ANN001
    return client.get("/api/chat/conversations").json()


def _one(client, cid: str) -> dict:  # noqa: ANN001
    return next(c for c in _folders(client)["conversations"] if c["id"] == cid)


def test_folder_round_trip(client) -> None:  # noqa: ANN001
    """建分组 → 会话挪进去 → 改名，整棵子树（含会话）跟着走。"""
    _register(client)
    cid = _new_conversation(client)

    resp = client.post("/api/chat/folders", json={"path": "考研/数学"}, headers=_headers(client))
    assert resp.status_code == 200, resp.text
    # 父级要一起建：只建 `a/b` 的话，树里会多出一层没有名字的中间层
    assert _folders(client)["folders"][:2] == ["考研", "考研/数学"]

    moved = client.patch(f"/api/chat/conversations/{cid}", json={"folder": "考研/数学"}, headers=_headers(client))
    assert moved.status_code == 200, moved.text
    assert _one(client, cid)["folder"] == "考研/数学"

    renamed = client.patch(
        "/api/chat/folders", json={"path": "考研", "to": "2027 考研"}, headers=_headers(client)
    )
    assert renamed.status_code == 200, renamed.text
    listed = _folders(client)
    assert "2027 考研/数学" in listed["folders"]
    # 会话也跟着走了 —— 只改分组记录不改会话，是这里最容易漏的一半
    assert _one(client, cid)["folder"] == "2027 考研/数学"


def test_empty_folder_survives(client) -> None:  # noqa: ANN001
    """空分组要留在列表里。

    只从会话反推目录的话，"新建分组"点完界面上什么都没有 —— 用户只会以为坏了。
    """
    _register(client)
    assert client.post("/api/chat/folders", json={"path": "待整理"}, headers=_headers(client)).status_code == 200
    assert "待整理" in _folders(client)["folders"]


def test_folder_refuses_to_swallow_another(client) -> None:  # noqa: ANN001
    """改成已存在的分组名：**拒绝**，不悄悄合并（并完用户就找不到东西了）。"""
    _register(client)
    for name in ("A", "B"):
        assert client.post("/api/chat/folders", json={"path": name}, headers=_headers(client)).status_code == 200
    resp = client.patch("/api/chat/folders", json={"path": "A", "to": "B"}, headers=_headers(client))
    assert resp.status_code == 409, resp.text


def test_folder_cannot_move_into_itself(client) -> None:  # noqa: ANN001
    """`A` 挪进 `A/B` 会让整棵子树从界面上消失（自己成了自己的孩子）。"""
    _register(client)
    assert client.post("/api/chat/folders", json={"path": "A/B"}, headers=_headers(client)).status_code == 200
    resp = client.patch("/api/chat/folders", json={"path": "A", "to": "A/B"}, headers=_headers(client))
    assert resp.status_code == 400, resp.text


def test_folder_delete_refuses_when_not_empty(client) -> None:  # noqa: ANN001
    """删分组：里面还有东西就拒绝（会话是最贵的产物，一个误点不该带走一堆）。"""
    _register(client)
    cid = _new_conversation(client)
    client.post("/api/chat/folders", json={"path": "有东西"}, headers=_headers(client))
    client.patch(f"/api/chat/conversations/{cid}", json={"folder": "有东西"}, headers=_headers(client))

    resp = client.delete("/api/chat/folders", params={"path": "有东西"}, headers=_headers(client))
    assert resp.status_code == 400, resp.text
    assert "有东西" in _folders(client)["folders"]
    # 清空之后才删得掉
    client.patch(f"/api/chat/conversations/{cid}", json={"folder": ""}, headers=_headers(client))
    assert client.delete("/api/chat/folders", params={"path": "有东西"}, headers=_headers(client)).status_code == 200
    assert "有东西" not in _folders(client)["folders"]


# ------------------------------------------------------------------ 分组 ⟺ 归档
#
# 一条不变量：**在分组里 ⟹ 已归档**。分组住在归档区（左栏那棵树只渲染
# `archived` 的会话），所以"在分组里却没归档"这个组合在界面上是**隐形**的 ——
# 会话跑到"未归档"区去了、分组看着是空的，可删分组时后端又照 `folder` 把它
# 算进去。用户报的"分组里还有 7 个子对话，删不掉"就是它（`考研/数学`）。


def test_new_conversation_inside_a_folder_is_archived(client) -> None:  # noqa: ANN001
    """在分组里点「新对话」：落进分组，**并且归档**。

    少了归档那一半，新会话在树上根本不出现（它落在未归档区），而分组看着是空的。
    """
    _register(client)
    created = client.post(
        "/api/chat/conversations", json={"folder": "考研/数学"}, headers=_headers(client)
    ).json()["conversation"]
    assert created["folder"] == "考研/数学"
    assert created["archived"] is True, created
    assert _one(client, created["id"])["archived"] is True


def test_unarchive_also_leaves_the_folder(client) -> None:  # noqa: ANN001
    """取消归档 = 同时出分组。

    反过来的那一半：不能一边"在分组里"一边"未归档"。
    """
    _register(client)
    cid = _new_conversation(client)
    client.patch(f"/api/chat/conversations/{cid}", json={"folder": "A/B"}, headers=_headers(client))
    assert _one(client, cid)["archived"] is True

    client.patch(f"/api/chat/conversations/{cid}", json={"archived": False}, headers=_headers(client))
    row = _one(client, cid)
    assert row["archived"] is False
    assert row["folder"] == "", row


def test_heals_a_conversation_left_in_a_folder_without_archive(client, db_session) -> None:  # noqa: ANN001
    """旧数据「在分组里却没归档」：读列表时自愈，让它回到树上。

    这是旧代码造出来的行（`create_conversation` 收下 `folder` 时没同时归档）。
    不治的话它既不在树上、又占着分组 —— 分组删不掉，用户也不知道东西在哪。
    """
    from app.models import Conversation

    _register(client)
    cid = _new_conversation(client)
    client.patch(f"/api/chat/conversations/{cid}", json={"folder": "考研/数学"}, headers=_headers(client))

    # 直接改库，模拟旧代码留下的那一行（现在的接口已经不会这么写了）
    conv = db_session.get(Conversation, uuid.UUID(cid))
    conv.archived = False
    db_session.commit()

    assert _one(client, cid)["archived"] is True  # 这一次读列表顺手治了

    # 治回来之后才删得掉：先把它挪出分组
    client.patch(f"/api/chat/conversations/{cid}", json={"folder": ""}, headers=_headers(client))
    resp = client.delete("/api/chat/folders", params={"path": "考研/数学"}, headers=_headers(client))
    assert resp.status_code == 200, resp.text


def test_folder_path_is_cleaned(client) -> None:  # noqa: ANN001
    """`..` / 空段 / 首尾斜杠一律清掉 —— 不许在树里长出一个叫 `..` 的目录。

    这一层只是字符串（不是真文件系统），留着它只会在界面上多一个谁也说不清的层级。
    """
    _register(client)
    cid = _new_conversation(client)
    resp = client.post("/api/chat/folders", json={"path": " /考研//..//数学/ "}, headers=_headers(client))
    assert resp.status_code == 200, resp.text
    assert resp.json()["path"] == "考研/数学"
    client.patch(f"/api/chat/conversations/{cid}", json={"folder": "a/../b"}, headers=_headers(client))
    assert _one(client, cid)["folder"] == "a/b"


# ------------------------------------------------------------------ 模式


def test_message_carries_the_mode_snapshot(client, monkeypatch) -> None:  # noqa: ANN001
    """每条用户消息记下**发送那一刻挂载了哪几组工具**（对话树据此画模式变化）。

    模式是随时可改的：改完之后"当时是怎么问的"就再也推不出来了 ——
    而回看对话时那恰恰是最要紧的一半（同一句话，挂了资料库与只有极简模式，
    答案的口径完全不同）。所以判据是"与上一条不同"，不是"当前是什么"。
    """
    _ready(client)
    _stub(monkeypatch, _happy_stream())
    cid = _new_conversation(client)

    mounted = client.get("/api/chat/mounts").json()["mounted"]
    assert mounted, "前提：默认（没配过）是全都挂上"

    _send(client, cid, content="第一句")
    messages = client.get(f"/api/chat/conversations/{cid}").json()["messages"]
    first = next(one for one in messages if one["role"] == "user")
    assert sorted(first["mounts"]) == sorted(mounted)

    # 切成极简模式再问一句：两条的模式必须不一样，否则"变化"画不出来
    resp = client.post("/api/chat/mounts", json={"groups": []}, headers=_headers(client))
    assert resp.status_code == 200, resp.text
    _send(client, cid, content="第二句")

    users = [one for one in client.get(f"/api/chat/conversations/{cid}").json()["messages"] if one["role"] == "user"]
    assert len(users) == 2
    assert users[0]["mounts"] != users[1]["mounts"]
    assert users[1]["mounts"] == []


def test_no_mode_lets_it_assume_what_he_is_studying() -> None:
    """**每一个模式**的提示词里都要有"不许预设他在学什么"。

    来历：这条禁令原来只写在极简那份（`MINIMAL_PROMPT`）里，挂了工具的那几份
    一条都没写 —— 于是学习模式下一句"你好"，它回的是"让我查 circular buffer、
    NOC、tile 布局…"（那些词全是从材料库里捡的，用户根本没提过）。
    用户的原话："无论是什么模式，你都不能预设用户在学什么。"

    顺带钉住领域那句的**说法**：材料库的主题必须被说成"材料的范围"，
    而不是"他在学什么" —— 老版本写的是"服务一个正在啃 AI 加速器的学习者"，
    模型就是顺着那句话开始猜的。
    """
    from app.routers import chat as chat_router

    every = set(chat_router.GROUP_PROMPTS.keys())
    for mounts in (set(), {"notes"}, every):
        prompt = chat_router.build_prompt(mounts, None, ["notes"])
        assert "不许预设他在学什么" in prompt, "禁令每个模式都要有"
        assert "服务一个正在啃" not in prompt, "旧人设（'正在啃 AI 加速器的学习者'）不许再出现"

    # 挂了工具的那些还要多一句"材料≠他"的说明；极简不提材料库，所以不要求它
    assert "不等于**他**在学什么" in chat_router.build_prompt(every, None, ["notes"])


def test_a_long_message_is_not_silently_cut(client, monkeypatch) -> None:  # noqa: ANN001
    """贴一篇文章进去，**原样存下来** —— 不许悄悄砍掉后半截。

    这条抓的是一个真事故：`MAX_CONTENT` 原来是 8000 字。用户贴了一篇 RL-Kernel
    的稿子，库里那条消息正好 8000 字、后半没了，而**界面上没有任何提示** ——
    他最后是从模型嘴里知道的（模型说"你稿子最后一句被截断了"）。
    8000 字对"贴一段材料"也许够，对"贴一篇文章"远远不够。
    """
    _ready(client)
    _stub(monkeypatch, _happy_stream())
    cid = _new_conversation(client)

    text = "一" * 20000
    _send(client, cid, content=text)

    users = [
        one
        for one in client.get(f"/api/chat/conversations/{cid}").json()["messages"]
        if one["role"] == "user"
    ]
    assert len(users) == 1
    assert users[0]["content"] == text, "整篇要原样存下来（8000 那会儿会被切成 8000）"


def test_new_conversation_lands_in_its_folder(client) -> None:  # noqa: ANN001
    """在分组上新建的对话就该在那个分组里（不必"建在根上再拖进去"）。

    分组也顺带登记 —— 否则左栏树里那个分组会时隐时现（树是"分组表 + 会话的
    folder"一起才画得出来的）。
    """
    _register(client)
    resp = client.post("/api/chat/conversations", json={"folder": "考研/数学"}, headers=_headers(client))
    assert resp.status_code == 200, resp.text
    assert resp.json()["conversation"]["folder"] == "考研/数学"
    assert "考研/数学" in _folders(client)["folders"]


# ------------------------------------------------------------------ 删掉一支


def _tree(client, db_session):  # noqa: ANN001
    """造一棵**有分叉**的小树，返回 `(cid, {名字: 消息 id})`。

    形状：`回答一` 下面分两支 —— 甲那支是要删的，乙那支一条都不该动：

        第一问 ─ 回答一 ─┬─ 追问甲 ─ 回答甲   ← 删「追问甲」
                         └─ 追问乙 ─ 回答乙
    """
    from app.models import Conversation, Message

    _ready(client)
    cid = _new_conversation(client)
    conv = db_session.get(Conversation, uuid.UUID(cid))
    ids: dict = {}

    def add(name: str, parent, role: str, text: str):  # noqa: ANN001
        row = Message(
            conversation_id=conv.id,
                parent_id=parent,
            role=role,
            content=text,
            status="ok",
        )
        db_session.add(row)
        db_session.commit()
        ids[name] = row.id
        return row.id

    q1 = add("q1", None, "user", "第一问")
    a1 = add("a1", q1, "assistant", "回答一")
    ua = add("ua", a1, "user", "追问甲")
    add("aa", ua, "assistant", "回答甲")
    ub = add("ub", a1, "user", "追问乙")
    add("ab", ub, "assistant", "回答乙")
    return cid, ids


def test_delete_message_takes_the_whole_branch(client, db_session) -> None:  # noqa: ANN001
    """删一条 = 删掉**以它为根的那棵子树**；另一支与上游一条都不动。

    这是对话树上「删掉这一支」背后的语义。为什么只有这一种：树上留一个
    `parent_id` 指向已删消息的孩子，就是**指不到根的孤枝** —— 读会话、算当前
    分支、画树三处会各自崩一次。所以没有"只删这一个、把孩子留下"这个选项。

    这条同时**实测了级联删除真的生效**：靠的是 `messages.parent_id` 上的
    `ondelete="CASCADE"` 加上引擎启动时那句 `PRAGMA foreign_keys=ON`（`db.py`）。
    `foreign_keys` 是**每连接**的开关、SQLite 默认是关的 —— 哪一天那句 PRAGMA
    被删掉，这个测试会立刻红，而不是等到用户发现删了下游还在。
    """
    from sqlalchemy import select

    from app.models import Message

    cid, ids = _tree(client, db_session)

    res = client.delete(
        f"/api/chat/conversations/{cid}/messages/{ids['ua']}", headers=_headers(client)
    )
    assert res.status_code == 200, res.text
    assert res.json()["deleted"] == 2, "「追问甲」与它下面的「回答甲」一起走"

    db_session.expire_all()
    left = {
        row.id
        for row in db_session.scalars(
            select(Message).where(Message.conversation_id == uuid.UUID(cid))
        )
    }
    assert left == {ids["q1"], ids["a1"], ids["ub"], ids["ab"]}, "另一支与上游不许动"


def test_delete_message_refuses_unknown_and_foreign(client, db_session) -> None:  # noqa: ANN001
    """不存在的 id、**另一个会话**里的 id —— 都 404。

    后半条是重点：`mid` 是路径上的裸数字。不校验归属的话，拿着 A 会话的 id 去
    B 会话那条路径上删，会**删掉另一个对话里的东西** —— 而且从 URL 上看不出来。
    """
    from sqlalchemy import select

    from app.models import Message

    cid, _ids = _tree(client, db_session)
    other, other_ids = _tree(client, db_session)

    missing = client.delete(
        f"/api/chat/conversations/{cid}/messages/99999999", headers=_headers(client)
    )
    assert missing.status_code == 404

    foreign = client.delete(
        f"/api/chat/conversations/{cid}/messages/{other_ids['q1']}", headers=_headers(client)
    )
    assert foreign.status_code == 404, "别的会话里的消息，不许从这条路径删"

    db_session.expire_all()
    still = db_session.scalars(
        select(Message).where(Message.conversation_id == uuid.UUID(other))
    ).all()
    assert len(still) == 6, "另一个会话必须一条不少"

def test_the_trail_dashboard_rides_along_in_the_system_prompt(client, monkeypatch) -> None:  # noqa: ANN001
    """「他的学习轨迹」进的是**每一轮的系统提示**，而且**不再是一个工具**。

    它原来是工具（`read_learning_tree`，要模型自己想起来去调）；用户："我认为它不应该
    被做成工具，对话轨迹是 llm 长期可见的用户信息仪表盘。" 所以这条断言落在两个地方：
    上游收到的第一条 system 里有没有它、工具声明里还有没有它。
    """
    seen: list[list[dict]] = []
    tool_names: list[list[str]] = []

    def fake(conf, messages, *, tools=None, params=None):
        seen.append(messages)
        tool_names.append([one["function"]["name"] for one in (tools or [])])
        yield ("delta", "好")
        yield ("finish", "stop")

    _ready(client)
    _stub(monkeypatch, fake)
    cid = _new_conversation(client)
    _send(client, cid, content="先聊一句，铺一条主线")
    _send(client, cid, content="那缓存行是什么？")

    system = seen[-1][0]["content"]
    assert seen[-1][0]["role"] == "system"
    assert "## 他的学习轨迹" in system, "仪表盘每一轮都在"
    assert "缓存行" in system, "他自己刚说的那句要在上面（这是他的轨迹，不是泛泛的模板）"
    assert "read_learning_tree" not in system
    assert all("read_learning_tree" not in names for names in tool_names), "它不该再出现在工具声明里"
