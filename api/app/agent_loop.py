"""agent 循环 —— 模型说、要工具、拿到结果、接着说，直到它不再要。

## 为什么单独一个模块

这是整个项目里唯一一处"我们自己写循环"的地方，也是将来换 SDK 时唯一要重写的地方。
LibreChat 的对应决定值得抄：它把循环整个外包给 `@librechat/agents`（LangGraph 系），
宿主只提供工具回调与事件流 —— 分层是对的，只是那个包是 Node 的，我们用不上，
于是在 Python 里写一个够用的：**宿主（路由）只做两件事，喂工具表、把事件流出去。**

## 上限都是硬的（模型不会替你省）

* 最多 `MAX_TURNS` 轮，每轮一次上游调用 —— 防"工具互相喂"把额度烧光
* 单次工具输出 `parts.MAX_TOOL_OUTPUT` 字 —— 防一次查询把上下文撑爆
* 上下文预算：从**最新往前**塞，超出就丢最旧的并留一句话说明。
  这是 LibreChat 的做法（`BaseClient.getMessagesWithinTokenLimit`），
  它比"从头塞到满"要好：正在聊的那几句一定在窗口里。

## 历史里要不要回放工具调用

要，而且**已经在做**：`routers/chat.py` 的 `_history()` 会把 `tool_call` 零件
还原成 assistant + tool 两条消息。

这条曾经是个"先不做"的取舍（工具输出比对话还长，怕上下文膨胀）。它的代价
实测出来了：模型看不见自己调过什么，历史读起来就是「我说了句『让我查一下』，
然后直接给结论」—— 于是它照着学，也先交代一句、然后什么都不做就停下
（问「考我一道」，一半次数只得到一句英文承诺）。预算机制按"从最新往前塞"裁剪、
工具输出各自截断，膨胀是可控的。
"""

from __future__ import annotations

import json
import time

from . import ai_gateway as gateway
from .parts import clip

MAX_TURNS = 6
DEFAULT_BUDGET = 8000
# 单条历史消息的固定开销（角色、分隔符），粗估即可
_MSG_OVERHEAD = 4

# 上游报 400 且报文里提到这些词 → 八成是"这个模型不支持工具调用"。
# 与设置面板里 `response_format` 的降级重试是同一条思路：供应商做不到，
# 就退回能做到的那一档，而不是把一个 400 甩给用户。
#
# 判据必须**具体到"这个参数不认识"**，不能只看到 "tool" 就认。
# 踩过的坑：`Messages with role 'tool' must be a response to a preceding message
# with 'tool_calls'` 里也含 `tool_calls`，于是被判成"不支持工具调用" ——
# 那其实是消息序列不合法（截断把 assistant 与 tool 拆开了），降级重试只会用
# 同样坏的历史再撞一次，还把用户引去查模型（错误的方向）。
_NO_TOOLS_HINTS = (
    "unknown parameter: tools",
    "unrecognized request argument",
    "unexpected keyword argument",
    "does not support tools",
    "tools is not supported",
    "tool_choice is not supported",
    "不支持 tools",
)


def _tools_unsupported(exc) -> bool:  # noqa: ANN001
    if exc.status != 400:
        return False
    detail = (exc.detail or "").lower()
    return any(hint in detail for hint in _NO_TOOLS_HINTS)


def estimate_tokens(text: str) -> int:
    """粗估 token 数：中日韩字符按 1 个、其余按 3 字符 1 个。

    **故意偏高。** 低估的代价是被供应商从中间截断 —— 用户看到半截回答，
    而我们甚至不知道它被截了；高估的代价只是少放几条历史。
    "预算"这件事本来就该往安全那边偏。
    """
    src = text or ""
    wide = 0
    for ch in src:
        code = ord(ch)
        if 0x3400 <= code <= 0x9FFF or 0x3040 <= code <= 0x30FF or 0xFF00 <= code <= 0xFFEF:
            wide += 1
    return wide + (len(src) - wide + 2) // 3 + 1


def _units(history: list[dict]) -> list[list[dict]]:
    """把历史切成**不能再拆的组**：带 `tool_calls` 的 assistant 与紧跟它的那几条
    `tool` 回复是一组。

    为什么必须成组：上游只接受「`tool` 消息紧跟在它对应的 `tool_calls` 后面」。
    按条截断会正好落在这一组中间，于是请求的第一条就成了 `role: tool` ——
    实测就是这个症状（DeepSeek 直接 400：Messages with role 'tool' must be
    a response to a preceding message with 'tool_calls'）。
    """
    units: list[list[dict]] = []
    index = 0
    while index < len(history):
        message = history[index]
        if message.get("role") == "assistant" and message.get("tool_calls"):
            unit = [message]
            index += 1
            while index < len(history) and history[index].get("role") == "tool":
                unit.append(history[index])
                index += 1
            units.append(unit)
            continue
        units.append([message])
        index += 1
    return units


def build_messages(system: str, history: list[dict], budget: int) -> tuple[list[dict], int]:
    """当前分支 → 上游要的 messages，并按预算截断。返回 (messages, 丢掉条数)。

    截断的单位是**组**而不是条（见 `_units`）：从最新往回塞，塞不下就整组不塞。
    另外兜一道：万一历史本身的第一条就是孤立的 `tool`（理论上不该出现），
    也把它丢掉 —— 这种请求上游一定拒，不如我们自己先修好。
    """
    kept_units: list[list[dict]] = []
    used = estimate_tokens(system)
    for unit in reversed(_units(history)):
        cost = sum(
            estimate_tokens(str(message.get("content") or "")) + _MSG_OVERHEAD
            for message in unit
        )
        if kept_units and used + cost > budget:
            break
        used += cost
        kept_units.append(unit)

    kept = [message for unit in reversed(kept_units) for message in unit]
    while kept and kept[0].get("role") == "tool":
        kept.pop(0)

    dropped = len(history) - len(kept)
    head = system
    if dropped:
        head += (
            "\n\n（这次对话更早的 "
            + str(dropped)
            + " 条消息因长度限制没有带进来，需要时可以请用户重述。）"
        )
    return [{"role": "system", "content": head}, *kept], dropped


def _parse_args(raw) -> dict:  # noqa: ANN001
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def run(  # noqa: ANN001
    db,
    user,
    conf,
    *,
    system: str,
    history: list[dict],
    tools,
    tool_context: dict | None = None,
    budget=None,
    max_turns=MAX_TURNS,
):
    """驱动若干轮，产出事件字典（宿主按 kind 分发）：

        {"kind":"note", ...}                        历史被截断
        {"kind":"text","text":...}                  正文增量
        {"kind":"think","text":...}                 推理增量
        {"kind":"tool_start","callId","name","args"}
        {"kind":"tool_result","callId","name","args","ok","ms","output"}
        {"kind":"error","message","detail","retryable"}
        {"kind":"finish","reason","usage"}
    """
    budget = int(budget or conf.get("maxContextTokens") or DEFAULT_BUDGET)
    messages, dropped = build_messages(system, history, budget)
    if dropped:
        yield {"kind": "note", "text": "更早的 " + str(dropped) + " 条消息没有带进来"}

    usage = {"promptTokens": 0, "completionTokens": 0}
    allow_tools = True

    for _turn in range(max_turns):
        chunks: list[str] = []
        calls: list[dict] = []
        finish = ""

        while True:
            try:
                # 最后一轮**不给工具**：预算用尽时，模型手里已经有检索来的东西了，
                # 该收口作答。不给它这个机会的话，它会把最后一轮也花在工具上 ——
                # 实测症状是：问"该先补什么"，六轮全用在检索，用户最后只拿到
                # 一堆工具气泡和一句"我先把这些核一遍"，一个字结论都没有。
                #
                # 条件写在调用处（而不是循环外算一次）：降级重试时 `allow_tools`
                # 会变，算在外面的话重试还会带着工具上去，等于没降级。
                for kind, value in gateway.stream_completion(
                    conf,
                    messages,
                    tools=tools.specs() if (allow_tools and _turn < max_turns - 1) else None,
                ):
                    if kind == "delta":
                        chunks.append(value)
                        yield {"kind": "text", "text": value}
                    elif kind == "think":
                        yield {"kind": "think", "text": value}
                    elif kind == "tool_calls":
                        calls = value
                    elif kind == "usage":
                        usage["promptTokens"] += int(value.get("promptTokens") or 0)
                        usage["completionTokens"] += int(value.get("completionTokens") or 0)
                    elif kind == "finish":
                        finish = value
                break
            except gateway.UpstreamError as exc:
                # 不支持工具调用的模型：摘掉工具重来一次（必须还没吐出任何字，
                # 否则重来会把同一段话发两遍）
                if allow_tools and not chunks and _tools_unsupported(exc):
                    allow_tools = False
                    yield {"kind": "note", "text": "这个模型不支持工具调用，已改为直接回答"}
                    continue
                yield {
                    "kind": "error",
                    # 连不上时要说"下一步干什么"：自己填密钥的人去查密钥与网络，
                    # 走内测通道的人要去把本地模型起起来 —— 两种动作完全不同
                    "message": gateway.connection_hint(conf)
                    if exc.kind == "connect"
                    else str(exc),
                    "detail": exc.detail,
                    "retryable": exc.kind in ("timeout", "connect"),
                }
                return

        if not calls:
            yield {"kind": "finish", "reason": finish or "stop", "usage": usage}
            return

        # 模型要工具：先把这一轮（它说的话 + 它要的调用）记进上下文，
        # 否则下一轮它看不到自己刚才要求过什么
        messages.append(
            {
                "role": "assistant",
                "content": "".join(chunks),
                "tool_calls": [
                    {
                        "id": call["id"] or "call_" + str(index),
                        "type": "function",
                        "function": {
                            "name": call["name"],
                            "arguments": call["arguments"] or "{}",
                        },
                    }
                    for index, call in enumerate(calls)
                ],
            }
        )

        for index, call in enumerate(calls):
            call_id = call["id"] or "call_" + str(index)
            args = _parse_args(call["arguments"])
            yield {"kind": "tool_start", "callId": call_id, "name": call["name"], "args": args}

            started = time.perf_counter()
            ok, payload = tools.call(db, user, call["name"], args, tool_context)
            elapsed = gateway.elapsed_ms(started)
            text = clip(tools.output_text(payload))
            yield {
                "kind": "tool_result",
                "callId": call_id,
                "name": call["name"],
                "args": args,
                "ok": ok,
                "ms": elapsed,
                "output": text,
                # 原样的结果也给宿主一份：有的结果不只给模型看，还要变成零件
                # （题卡就是：模型看到题面，界面渲染成可作答的卡）
                "payload": payload,
            }
            messages.append({"role": "tool", "tool_call_id": call_id, "content": text})

    # 轮数用尽：这是"模型在打转"，不是上游故障，所以要明确说出来
    yield {
        "kind": "error",
        "message": "工具调用达到上限（" + str(max_turns) + " 轮）",
        "detail": "模型反复要求调用工具，已经停下。把问题问得更具体一些再试。",
        "retryable": False,
    }
    yield {"kind": "finish", "reason": "tool_limit", "usage": usage}
