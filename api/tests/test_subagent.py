"""子代理：**把一条长工具链派出去，只把结果带回来**。

形状照的是 coding agent 里那个 `Task` 工具（本仓库的开发就是这么用的）：
调用方给一条**任务描述**，子代理在自己的上下文里跑完（查几次、换什么词由它定），
交回一份结果 —— 中间过程一条都不进主对话。

这里钉住四条边界（对应 `draft/f.md` 里那段讨论）：

  1. **它不比主对话多一分权**：`allow` 是只读就只读，`mounts` 挂哪几组就哪几组；
  2. **它不许再派子代理**（递归一旦开口子，轮数预算失控），声明里摘掉、报名字也拦住；
  3. **回报原样进主线**（原文摘录不该被 JSON 转义吃掉），而不是压成一句摘要；
  4. **失败显式**：它没交回正文，就要说"没交回正文"，不能装成"材料里没有"。

不靠模型：网关是伪造的（与 `test_agent_wait.py` 同一套手法）。
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app import agent_loop, subagent, tools  # noqa: E402

REPORT = (
    "## 结论\n"
    "环形缓冲那一节讲的是「读写指针不回绕」。\n\n"
    "## 依据\n"
    "- tt-metal/手册.md 第 179 行：`the pointer must be written back to L1`\n"
)


def _names_in(specs) -> list[str]:
    return [str(((one.get("function") or {}).get("name")) or "") for one in (specs or [])]


def test_subagent_gets_the_same_scope_and_cannot_recurse(db_session, local_user, monkeypatch):
    """一次真调用：走 `tools.call` 这个入口，验范围照抄与防递归。"""
    seen: list[list[str]] = []

    # 第一次进入子代理时交回报；子代理内部那轮（它报被禁的名字）之后收口
    inner_state = {"turns": 0}

    def stream(conf, messages, tools=None, params=None):  # noqa: A002
        names = _names_in(tools)
        seen.append(names)
        system = str((messages or [{}])[0].get("content") or "")
        if "子代理" in system:
            inner_state["turns"] += 1
            if inner_state["turns"] == 1:
                # 子代理想再派一个（应当被拦下），并且顺手要跑沙箱（也该被拦）
                yield (
                    "tool_calls",
                    [
                        {"id": "s1", "name": "run_subagent", "arguments": '{"task": "再来一个"}'},
                        {"id": "s2", "name": "run_python", "arguments": '{"code": "print(1)"}'},
                    ],
                )
                yield ("finish", "tool_calls")
                return
            yield ("delta", REPORT)
            yield ("finish", "stop")
            return
        # 主对话两轮：第一轮派活，第二轮读回报收口
        tool_msgs = [one for one in messages if one.get("role") == "tool"]
        if not tool_msgs:
            yield ("tool_calls", [{"id": "m1", "name": "run_subagent", "arguments": '{"task": "把环形缓冲钉到原文"}'}])
            yield ("finish", "tool_calls")
            return
        yield ("delta", "收到。")
        yield ("finish", "stop")

    monkeypatch.setattr(agent_loop.gateway, "stream_completion", stream)
    monkeypatch.setattr(
        subagent.gateway, "resolve_config", lambda db, uid: {"apiKey": "x", "model": "fake"}
    )

    mounts = {"notes", "library", "graph"}
    ok, payload = tools.call(
        db_session,
        local_user,
        "run_subagent",
        {"task": "把环形缓冲钉到原文"},
        {"conversationId": 1},
        mounts=mounts,
        allow=("read",),
    )
    assert ok, payload
    assert "subagent" in payload, payload

    # 1) **元能力**在该在的声明里（直接问 specs：这条测试没有跑"主对话"那一轮 ——
    #    它是直接把子代理当工具调的，所以 `seen` 里全是**子代理自己的**轮次）
    assert "run_subagent" in _names_in(tools.specs(mounts, ("read",))), "挂了工具时元能力该在"
    assert "run_subagent" not in _names_in(tools.specs(set(), ("read",))), "极简模式里不该有"
    # 而子代理自己那几轮**一次都没有**它，也没有沙箱
    assert seen, "子代理该调过网关"
    assert all("run_subagent" not in names for names in seen), "子代理不许再派子代理"
    assert all("run_python" not in names for names in seen), "沙箱也不该给它（没人去跑）"

    # 2) 它想再派一个、想跑沙箱 —— 都被挡住（报名字也没用）
    blocked = payload["subagent"]["report"]
    assert isinstance(blocked, str)

    # 3) 回报**原样**进模型看到的那段文本（`output_text` 就是喂给模型的东西）：
    #    原文摘录一字不少，也没被 JSON 转义成一团 \n
    text = tools.output_text(payload)
    assert "the pointer must be written back to L1" in text
    assert "\\n" not in text, "不该是 JSON 转义过的一团"
    assert text.count("## 结论") == 1
    assert subagent.REPORT_MAX >= len(REPORT)


def test_only_the_final_words_are_the_delivery(db_session, local_user, monkeypatch):
    """子代理边查边念叨的那些话**不进报告** —— 只有收尾那一段算交付。

    实测撞到过：一次 24 次工具调用的检索，独白全挤进报告，把"结论 + 依据"
    挤出 4000 字的上限，界面上只剩一屏过程碎语。用户的原话：
    "这个东西是输入不是结果。"
    """
    inner = {"turns": 0}

    def stream(conf, messages, tools=None, params=None):  # noqa: A002
        system = str((messages or [{}])[0].get("content") or "")
        if "子代理" in system:
            inner["turns"] += 1
            n = inner["turns"]
            if n <= 2:
                # 中间轮：边查边念叨一句（这正是要丢掉的那种过程碎语）
                yield ("delta", "I'll start by locating concepts (turn " + str(n) + "). ")
                yield (
                    "tool_calls",
                    [{"id": "s" + str(n), "name": "search_material", "arguments": '{"query": "x"}'}],
                )
                yield ("finish", "tool_calls")
                return
            # 最后一轮**直接交**（system 里就是这么要求的："最后一次开口就是交付"）
            yield ("delta", "## 结论\n钉到了 179 行。")
            yield ("finish", "stop")
            return
        yield ("delta", "好。")
        yield ("finish", "stop")

    monkeypatch.setattr(agent_loop.gateway, "stream_completion", stream)
    monkeypatch.setattr(
        subagent.gateway, "resolve_config", lambda db, uid: {"apiKey": "x", "model": "f"}
    )

    ok, payload = tools.call(
        db_session,
        local_user,
        "run_subagent",
        {"task": "把那条钉到原文"},
        {"conversationId": 1},
        mounts={"library"},
        allow=("read",),
    )
    assert ok, payload
    report = payload["subagent"]["report"]
    assert "## 结论" in report and "179 行" in report
    assert "I'll start by locating" not in report, "过程独白不是交付"
    assert payload["subagent"]["calls"] == 2, "它调了几次照旧要报出来（那是可观测的）"
    # 面板上看到的就是这段（`output_text` 原样交出去）——"结论 + 依据"必须在
    shown = tools.output_text(payload)
    assert "## 结论" in shown and "I'll start by locating" not in shown


def test_subagent_says_so_when_it_brings_nothing_back(db_session, local_user, monkeypatch):
    """它只 finish、没正文 → 必须明说「没交回正文」，不许装成"材料里没有"。"""

    def stream(conf, messages, tools=None, params=None):  # noqa: A002
        system = str((messages or [{}])[0].get("content") or "")
        if "子代理" in system:
            yield ("finish", "stop")  # 一个字都不说
            return
        if not any(one.get("role") == "tool" for one in messages):
            yield ("tool_calls", [{"id": "m1", "name": "run_subagent", "arguments": '{"task": "查一下"}'}])
            yield ("finish", "tool_calls")
            return
        yield ("delta", "好，那我换个问法。")
        yield ("finish", "stop")

    monkeypatch.setattr(agent_loop.gateway, "stream_completion", stream)
    monkeypatch.setattr(
        subagent.gateway, "resolve_config", lambda db, uid: {"apiKey": "x", "model": "fake"}
    )

    ok, payload = tools.call(
        db_session,
        local_user,
        "run_subagent",
        {"task": "查一下"},
        {"conversationId": 1},
        mounts={"library"},
        allow=("read",),
    )
    assert ok, payload
    assert "没交回正文" in payload["note"], payload["note"]
    assert payload["subagent"]["report"] == ""


def test_a_plan_is_not_a_delivery(db_session, local_user, monkeypatch):
    """最后只说了一句"接下来我要去查 X" → **不算交付**，而且必须明说。

    实测七次真调用里有**三次**这么收场，报回来的是
    "Excellent — … Let me read it in different regions …"、
    "I have read all the target notes in the folder. Now let me verify …" —— 主线拿到
    这句话会以为任务结果就是这个，而真相是**它还没干完**（用户报的就是这一条：
    "子代理两次都没把报告交回来"）。

    所以两件事都要：
      * 这种话**不许挂在 `report` 名下**（名字就是承诺，名不副实比空着更糟）；
      * 但也不能丢 —— 换个名字带上，有时正好看出它卡在哪。
    """
    nudged: list[str] = []

    def stream(conf, messages, tools=None, params=None):  # noqa: A002
        system = str((messages or [{}])[0].get("content") or "")
        if "子代理" in system:
            if tools is None:
                # 最后一轮：工具被收走了。此时**应当**已经插了一句明示（`final_nudge`）
                nudged.append(
                    " ".join(
                        str(one.get("content") or "")
                        for one in messages
                        if one.get("role") == "user"
                    )
                )
                # 而它照样只念叨一句"接下来要做什么"（真事：模型察觉不到工具没了）
                yield ("delta", "I have read the notes. Now let me verify the formulas.")
                yield ("finish", "stop")
                return
            yield ("delta", "Let me read it in different regions. ")
            yield (
                "tool_calls",
                [{"id": "s1", "name": "search_material", "arguments": '{"query": "x"}'}],
            )
            yield ("finish", "tool_calls")
            return
        yield ("delta", "好。")
        yield ("finish", "stop")

    monkeypatch.setattr(agent_loop.gateway, "stream_completion", stream)
    monkeypatch.setattr(
        subagent.gateway, "resolve_config", lambda db, uid: {"apiKey": "x", "model": "fake"}
    )

    ok, payload = tools.call(
        db_session,
        local_user,
        "run_subagent",
        {"task": "把 SVD 到底记没记查清"},
        {"conversationId": 1},
        mounts={"library"},
        allow=("read",),
    )
    assert ok, payload
    sub = payload["subagent"]
    assert sub["delivered"] is False
    assert sub["report"] == "", "半句打算不许挂在 report 名下"
    assert "Now let me verify" in sub["lastWords"], "但它说了什么要带回去"
    assert "没交回回报" in payload["note"], payload["note"]

    # 面板与模型看到的那段要**写明这不是回报**
    shown = tools.output_text(payload)
    assert "不是回报" in shown and "Now let me verify" in shown
    # 收尾那句明示真的发出去了（`agent_loop.run` 的 `final_nudge`）——
    # 这是"病因"那一半：模型察觉不到工具被收走，就得有人告诉它
    assert nudged and "工具已经收回" in nudged[0], nudged


def test_a_long_report_is_cut_at_a_line_break(db_session, local_user, monkeypatch):
    """回报超长要在**换行处**截 —— 切在半句话上，读的人会以为正文到那儿就完了。

    实测那条就是这样："…未扫的 20 份被列在 `skipped` 字段，且" 后面直接接一句
    截断说明，看起来像原文只写到一半。
    """
    long = "## 依据\n" + "".join("- 第 %d 行：%s END\n" % (i, "x" * 120) for i in range(1, 80))
    assert len(long) > subagent.REPORT_MAX, "得真的超长，不然测不到截断"

    def stream(conf, messages, tools=None, params=None):  # noqa: A002
        system = str((messages or [{}])[0].get("content") or "")
        if "子代理" in system:
            yield ("delta", long)
            yield ("finish", "stop")
            return
        yield ("delta", "好。")
        yield ("finish", "stop")

    monkeypatch.setattr(agent_loop.gateway, "stream_completion", stream)
    monkeypatch.setattr(
        subagent.gateway, "resolve_config", lambda db, uid: {"apiKey": "x", "model": "fake"}
    )

    ok, payload = tools.call(
        db_session,
        local_user,
        "run_subagent",
        {"task": "交一份长报告"},
        {"conversationId": 1},
        mounts={"library"},
        allow=("read",),
    )
    assert ok, payload
    report = payload["subagent"]["report"]
    assert "回报太长" in report, "截了就要说清"
    body = report.split("\n\n（回报太长")[0]
    assert body.endswith("END"), "该停在某一行的行尾，不该切在行的中间"
    assert len(body) <= subagent.REPORT_MAX
