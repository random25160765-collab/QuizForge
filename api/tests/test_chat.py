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
import uuid
from datetime import datetime, timedelta, timezone

from app import agent_loop, ai_gateway, parts
from app.deps import CSRF_COOKIE
from app.models import Material

PASSWORD = "password-1234"


# ------------------------------------------------------------------ 脚手架


def _register(client) -> None:  # noqa: ANN001
    email = f"chat-{uuid.uuid4().hex[:10]}@example.com"
    client.cookies.clear()
    resp = client.post("/api/auth/register", json={"email": email, "password": PASSWORD})
    assert resp.status_code in (200, 201), resp.text


def _headers(client) -> dict:  # noqa: ANN001
    return {"X-CSRF-Token": client.cookies.get(CSRF_COOKIE) or ""}


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


def test_requires_login(client) -> None:  # noqa: ANN001
    assert client.get("/api/chat/conversations").status_code == 401


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
    from app.models import Conversation, Message, User
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
        conversation_id=conv.id, user_id=conv.user_id, role="user", content="问一句", status="ok"
    )
    assistant = Message(
        conversation_id=conv.id,
        user_id=conv.user_id,
        parent_id=None,
        role="assistant",
        content="",
        status="streaming",
    )
    db_session.add_all([user_msg, assistant])
    db_session.commit()
    user_row = db_session.get(User, conv.user_id)

    gen = chat._stream(
        db_session,
        user_row,
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
    from app.models import Conversation, Message, User
    from app.routers import chat

    _ready(client)
    cid = _new_conversation(client)
    conv = db_session.get(Conversation, uuid.UUID(cid))
    assistant = Message(
        conversation_id=conv.id,
        user_id=conv.user_id,
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
    user_row = db_session.get(User, conv.user_id)
    chat._finish(
        db_session,
        user_row,
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


def test_search_never_leaks_other_users_messages(client, monkeypatch) -> None:  # noqa: ANN001
    _ready(client)
    _stub(monkeypatch, _happy_stream())
    cid = _new_conversation(client)
    _send(client, cid, content="这是一句只该我自己看见的话")

    _register(client)  # 换一个账号
    body = client.get("/api/chat/search", params={"q": "只该我自己"}).json()
    assert body["items"] == [], "别人的消息一条都不该搜得到"


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
    demo = next(part for part in done["parts"] if part["type"] == "demo")
    run_id = demo.get("runId")
    assert run_id, "零件要带上 runId，宿主才认得出是哪次运行"

    # 认错 runId 时不该悄悄写坏零件
    bad = client.post(
        f"/api/chat/conversations/{cid}/messages/{done['id']}/run",
        json={"runId": "nope", "text": "x"},
        headers=_headers(client),
    )
    assert bad.status_code == 404

    # 前端回填（沙箱跑完 postMessage → POST 到这里）
    ok = client.post(
        f"/api/chat/conversations/{cid}/messages/{done['id']}/run",
        json={"runId": run_id, "ok": True, "text": "已就绪：numpy 1.26.4\n结果是 2\n"},
        headers=_headers(client),
    )
    assert ok.status_code == 200, ok.text

    # 存下来了 —— 刷新之后界面上还看得到
    stored = next(
        m for m in client.get(f"/api/chat/conversations/{cid}").json()["messages"] if m["id"] == done["id"]
    )
    part = next(p for p in stored["parts"] if p["type"] == "demo")
    assert part["run"]["text"].startswith("已就绪：numpy 1.26.4")
    assert part["run"]["ok"] is True

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


def test_an_attachment_belongs_to_its_owner(client, monkeypatch) -> None:  # noqa: ANN001
    """别人的附件按 404 处理 —— 与「别人的会话」同一条规矩。"""
    _ready(client)
    up = client.post(
        "/api/chat/attachments",
        files={"file": ("mine.txt", b"hello", "text/plain")},
        headers=_headers(client),
    )
    aid = up.json()["id"]
    assert client.get(f"/api/chat/attachments/{aid}").status_code == 200

    _register(client)  # 换一个账号
    assert client.get(f"/api/chat/attachments/{aid}").status_code == 404


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
        {"role": "user", "content": "第一问"},
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


# ------------------------------------------------------------------ 归属


def test_another_user_gets_404_not_403(client, monkeypatch) -> None:  # noqa: ANN001
    """别人的会话：404。

    403 会告诉攻击者「这个 id 是存在的，只是不归你」——
    多租户下这种确认本身就是泄露。
    """
    _ready(client)
    _stub(monkeypatch, _happy_stream())
    cid = _new_conversation(client)
    _send(client, cid, content="私有内容")

    _register(client)  # 换一个账号
    assert client.get(f"/api/chat/conversations/{cid}").status_code == 404
    assert client.post(f"/api/chat/conversations/{cid}/messages", json={"content": "偷看"}, headers=_headers(client)).status_code == 404
    assert client.delete(f"/api/chat/conversations/{cid}", headers=_headers(client)).status_code == 404
    assert client.get("/api/chat/conversations").json()["conversations"] == [], "列表里也不该出现"


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
            user_id=conv.user_id,
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
            user_id=conv.user_id,
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
