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
from . import runs
from .parts import clip

MAX_TURNS = 6
#: 没人给预算时用的兜底。**它不该是个小数字**：8000 那会儿一顿工具调用就装满了，
#: 于是历史被悄悄截掉（用户看到的"更早的 N 条消息没有带进来"就是这么来的）。
#: 现在的规矩是"按模型自己的窗口定"（见 `ai_gateway.budget_tokens`），
#: 这里只给一条没人配置时的宽底线 —— 1M，与实例上限同一个量级。
DEFAULT_BUDGET = 1_000_000
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

    ## 开头那一个单元**钉住**

    这份历史本来就是"**根 → 他正在问的那一条**"这条链（见 `routers/chat.py` 的
    `_chain`），**不是**"整条对话里最近的 N 条"。这一条差别要紧：回溯到根那一段时，
    模型看得见最早在干什么，因为"根"永远在链的第一位。实测他那条 45 层、46 条的链
    全带上 5.3 万 tokens，1M 的预算下一条都不丢。

    但**预算不够的时候**，"从最新往前塞"第一个丢的正是**根** —— 而文档把这一端
    写死了：

        **当前路径（根 → 我现在在哪）：全留**（这是"我在哪"，丢了就断线）
                                                —— `docs/对话树.md` §七

    所以先给开头的那个单元留位置，再按最新的往里塞。中途丢掉的那些**不告诉模型**
    （理由见下面那段注释：它会把这类报备当自己的话复述给用户）。
    """
    units = _units(history)
    head = units[:1]
    head_cost = (
        sum(
            estimate_tokens(str(message.get("content") or "")) + _MSG_OVERHEAD
            for message in head[0]
        )
        if head
        else 0
    )
    # 钉归钉，**不许把整份预算吃掉**：第一句就贴了一整篇文章（20 万字上限）时，
    # 把它钉住等于这一轮只剩那篇文章 —— 那比丢掉开头更糟。所以只在"开头占得下
    # 四分之一预算"时才钉；占不下的那些走老规矩（从最新往前塞）。
    if head_cost > max(budget // 4, 1):
        head = []
        head_cost = 0

    kept_units: list[list[dict]] = []
    used = estimate_tokens(system) + head_cost
    # 没钉住开头时，开头那一单元**照旧参与**"从最新往前塞"（它是老规矩里的最后一位，
    # 不许因为这段改动被无声地排除在外）。
    rest = units[1:] if head else units
    for unit in reversed(rest):
        cost = sum(
            estimate_tokens(str(message.get("content") or "")) + _MSG_OVERHEAD
            for message in unit
        )
        if kept_units and used + cost > budget:
            break
        used += cost
        kept_units.append(unit)

    kept = [message for unit in head for message in unit] + [
        message for unit in reversed(kept_units) for message in unit
    ]
    while kept and kept[0].get("role") == "tool":
        kept.pop(0)

    dropped = len(history) - len(kept)
    # **不告诉模型"历史被截了"**。原来这里会往 system 尾巴上贴一句
    # "（这次对话更早的 N 条消息因长度限制没有带进来，需要时可以请用户重述。）"，
    # 结果模型把它当自己的话复述出来 —— 用户看到一句莫名其妙的报备
    #（"更早的 16 条消息没有带进来"，原话：直接把这个告知删掉，不要再留）。
    # 截断这件事我们自己知道就够了：`dropped` 照旧返回，调用方要不要上报自己定。
    return [{"role": "system", "content": system}, *kept], dropped


def _parse_args(raw) -> dict:  # noqa: ANN001
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


#: 沙箱那张图随的 user 消息上的名字 —— `_strip_images` 靠它认"这是我要处理的"。
#: 用 `name` 这个**合法字段**做标记（不放自定义键：那会跟着请求发出去，
#: 严格的网关会 400）。
_FIGURE_NAME = "sandbox-figure"


def _strip_images(messages: list[dict]) -> None:
    """把**沙箱拍的旧图**从要发出去的消息里剥掉（原地改）。

    为什么需要：沙箱的图以 base64 回来（一张几十 KB ≈ 上万 token）。当轮模型得
    **看见**它（它有读图能力）—— 但下一轮再发时它就是"已经看过的东西"，
    随历史一轮轮重放纯属白烧上下文。

    **只认自己那一种**（`name == _FIGURE_NAME`）：附件里的图是用户发的、是历史的
    一部分，每轮都该在（那条路另有一套用例钉着，不能被这里误伤）。

    当轮刚拍到的那条还带 `_fresh`：留一次（顺手摘掉标记 —— 它不该出现在请求里），
    下一轮再走到这里就换成一句"这里原本有一张图"，并把名字清掉（幂等）。
    """
    for one in messages:
        content = one.get("content")
        if not isinstance(content, list) or one.get("name") != _FIGURE_NAME:
            continue
        if one.pop("_fresh", False):
            continue  # 本轮刚拍的：留着给模型看
        kept = [
            part
            for part in content
            if not (isinstance(part, dict) and part.get("type") == "image_url")
        ]
        kept.append({"type": "text", "text": "（这里原本有一张图，已经看过了。）"})
        one["content"] = kept
        # 换成普通消息：下一轮不再匹配，也就不会再包一层说明（幂等）
        one.pop("name", None)


def _specs_without(specs: list[dict], drop: tuple[str, ...]) -> list[dict]:
    """把 `drop` 里那几个工具从声明里摘掉（子代理用它挡住"派给自己"）。"""
    if not drop:
        return specs
    return [
        one
        for one in specs
        if str(((one.get("function") or {}).get("name")) or "") not in drop
    ]


def run(  # noqa: ANN001
    db,
    conf,
    *,
    system: str,
    history: list[dict],
    tools,
    tool_context: dict | None = None,
    budget=None,
    max_turns=MAX_TURNS,
    mounts: set[str] | None = None,
    allow: tuple[str, ...] | set[str] | None = None,
    drop: tuple[str, ...] = (),
    thinking: bool = False,
    final_nudge: str = "",
):
    """驱动若干轮，产出事件字典（宿主按 kind 分发）：

        {"kind":"note", ...}                        一句系统提示（少数情况才有）
        {"kind":"text","text":...}                  正文增量
        {"kind":"think","text":...}                 推理增量
        {"kind":"tool_start","callId","name","args"}
        {"kind":"tool_result","callId","name","args","ok","ms","output"}
        {"kind":"error","message","detail","retryable"}
        {"kind":"finish","reason","usage"}
    """
    budget = int(budget or conf.get("maxContextTokens") or DEFAULT_BUDGET)
    # 历史照旧按预算裁剪（见 `build_messages`），但**不再往外说**
    #（原来这里会产出一条"更早的 N 条消息没有带进来"的 note，界面在输入框上方
    # 摆一行，占着间距而用户拿它没有办法）。`dropped` 因此无人使用，接下划线。
    messages, _dropped = build_messages(system, history, budget)

    usage = {"promptTokens": 0, "completionTokens": 0}
    allow_tools = True
    #: 收尾那句"工具收走了，现在交付"只插一次（见循环里那处）
    nudged = False

    for _turn in range(max_turns):
        chunks: list[str] = []
        thoughts: list[str] = []
        calls: list[dict] = []
        finish = ""
        # 协议泄漏：模型把工具调用**写成了正文**（实测内测通道的 deepseek-chat 会这样，
        # 用户看到的就是一屏 `<||DSML|| invoke name="push_question">` 这种原文）。
        # 半角/全角的竖线都要认 —— 它两种都吐过。判定前要按住一小截尾巴：
        # 标记可能被切成两块分别到达，逐块替换会漏。
        # 竖线有**单有双、有半角有全角**：实测它吐的是 `<||DSML|| invoke …` 与
        # 全角的 `｜｜DSML｜｜` 两种。只写单竖线会全都漏过去（我自己就漏了一次）。
        leak_marks = (
            "<||DSML||",
            "｜｜DSML｜｜",
            "<|DSML|",
            "|<DSML|",
            "<|tool",
            "</|tool",
            "<|function",
        )
        hold = ""
        leaked = False

        def _marker_tail(text: str) -> int:
            """尾巴里"可能是标记开头"的那几个字符有多长。

            正常情况下是 0 —— 正文一个字都不按，立刻可见（原来我按了 14 字，
            结果「流到一半的内容已在库里」这类契约直接被我推迟了，测试就红了）。
            只有尾巴正好长成了某个标记的前缀时才按住它，等下一块来判定。
            """
            for size in range(min(len(text), 14), 0, -1):
                suffix = text[-size:]
                if any(mark.startswith(suffix) for mark in leak_marks):
                    return size
            return 0

        while True:
            try:
                # 最后一轮**不给工具**：预算用尽时，模型手里已经有检索来的东西了，
                # 该收口作答。不给它这个机会的话，它会把最后一轮也花在工具上 ——
                # 实测症状是：问"该先补什么"，六轮全用在检索，用户最后只拿到
                # 一堆工具气泡和一句"我先把这些核一遍"，一个字结论都没有。
                #
                # 条件写在调用处（而不是循环外算一次）：降级重试时 `allow_tools`
                # 会变，算在外面的话重试还会带着工具上去，等于没降级。
                # 最后一轮工具已经被收走（上面那句 `_turn < max_turns - 1`）——
                # 但模型**常常察觉不到**：它会说"让我再查一下"，于是交回来的是半句
                # **打算**而不是结论（实测子代理七次里有三次如此，报回来的是
                # "Let me do targeted searches across the library for the six keywords."，
                # 而主线会把它当成任务的结果）。这一句就是明说：工具没了，现在交东西。
                if final_nudge and _turn >= max_turns - 1 and not nudged:
                    nudged = True
                    messages.append({"role": "user", "content": final_nudge})
                # 发之前先把**旧图**剥掉（沙箱那张 base64 只给模型看一次，见 _strip_images）
                _strip_images(messages)
                for kind, value in gateway.stream_completion(
                    conf,
                    messages,
                    tools=(
                        _specs_without(tools.specs(mounts, allow), drop)
                        if (allow_tools and _turn < max_turns - 1)
                        else None
                    ),
                    # 思考开关（见 `gateway.thinking_params`）：**显式**写，两个方向
                    # 都不依赖上游默认。谁要思考谁传 `thinking=True` —— 现在只有主对话
                    # （由输入框那颗「深度思考」药丸决定，默认开）。子代理与大题批改
                    # 不传：它们的产出是给主模型看的中间结果，思考只会更慢更贵，
                    # 思维链也没人展示。
                    params=gateway.thinking_params(str(conf.get("model") or ""), thinking),
                ):
                    if kind == "delta":
                        if leaked:
                            continue  # 这一轮已判定是泄漏：剩下的正文全丢
                        hold += value
                        if any(mark in hold for mark in leak_marks):
                            leaked = True
                            hold = ""
                            yield {
                                "kind": "note",
                                "text": "模型把工具调用写进了正文（协议泄漏），这一段已丢弃、本轮不作数。",
                            }
                            continue
                        # 正常情况一个字都不按；只有尾巴像标记开头时才留一截等下一块
                        cut = _marker_tail(hold)
                        safe = hold[: len(hold) - cut] if cut else hold
                        hold = hold[len(safe):]
                        if safe:
                            chunks.append(safe)
                            yield {"kind": "text", "text": safe}
                    elif kind == "think":
                        # 思维链：一路流给界面（它进"思考"折叠块），一路攒起来 ——
                        # 下一轮请求要把它**原样回传**。DeepSeek 文档写得很硬：带 tools
                        # 时若不回传 `reasoning_content`，上游直接 400（2026-09-20 查证）。
                        # 实测：`deepseek-flash` 裸调就给思维链（思考模式默认开启、
                        # 默认 effort=high），而 `deepseek-chat` 这个遗留名一个字节都不给。
                        # 两种模型共用这条链：攒到就回传、攒不到就不带那个字段。
                        thoughts.append(value)
                        yield {"kind": "think", "text": value}
                    elif kind == "tool_calls":
                        calls = value
                    elif kind == "usage":
                        usage["promptTokens"] += int(value.get("promptTokens") or 0)
                        usage["completionTokens"] += int(value.get("completionTokens") or 0)
                    elif kind == "finish":
                        finish = value
                # 流结束：把按住的那截尾巴放出来（泄漏的话上面已经清空了）
                if hold and not leaked:
                    chunks.append(hold)
                    yield {"kind": "text", "text": hold}
                    hold = ""
                break
            except gateway.UpstreamError as exc:
                # 不支持工具调用的模型：摘掉工具重来一次（必须还没吐出任何字，
                # 否则重来会把同一段话发两遍）
                if allow_tools and not chunks and _tools_unsupported(exc):
                    allow_tools = False
                    yield {"kind": "note", "text": "这个模型不支持工具调用，已改为直接回答"}
                    continue
                # 出错也不能把那截尾巴吞掉：流里已经说过的话，用户就该看得见
                if hold and not leaked:
                    chunks.append(hold)
                    yield {"kind": "text", "text": hold}
                    hold = ""
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
        turn_message: dict = {
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
        # 思维链跟着这一轮回传（见上面的 think 分支）：DeepSeek 带 tools 时缺了它
        # 会 400。攒不到就**省掉这个字段** —— `deepseek-chat` 这类非思考入口永远
        # 攒不到，请求因此与从前逐字节一致。
        if thoughts:
            turn_message["reasoning_content"] = "".join(thoughts)
        messages.append(turn_message)

        for index, call in enumerate(calls):
            call_id = call["id"] or "call_" + str(index)
            args = _parse_args(call["arguments"])
            yield {"kind": "tool_start", "callId": call_id, "name": call["name"], "args": args}

            started = time.perf_counter()
            if call["name"] in drop:
                # **纵深防御**：声明里已经不给了，报名字也不执行 ——
                # 挡的是"子代理又派一个子代理"（递归一旦开口子，轮数预算就失控）。
                ok, payload = False, {"error": "这个工具在这一次调用里不可用。"}
            else:
                ok, payload = tools.call(
                    db, call["name"], args, tool_context, mounts=mounts, allow=allow
                )
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

            # **跑脚本要等它跑完再说话。**
            #
            # `run_python` 的代码是在**页面**的沙箱（Pyodide）里执行的：上面那次
            # `yield` 只是把零件发出去，此刻一个字都还没跑。用户的要求是
            # "让 agent 等命令返回了再说话" —— 所以这里停住，等页面把输出报回来
            # （几毫秒到几秒）。
            #
            # **顺序不能反**：这一停必须在 `yield` **之后** —— 页面正是拿到那个零件
            # 才知道要跑哪段代码；先停后会互相等，直接死锁。
            #
            # 等到之后把输出并进工具结果：模型拿到的是**真跑出来的东西**，而不是
            # "我已经交进去跑了、下一轮再看"这种空话（实测它就是这么说的）。没人报
            # （页面没开、脚本死循环）就超时认输，并如实告诉它没等到 —— 不许它编。
            demo = payload.get("demo") if isinstance(payload, dict) else None
            run_id = ""
            if isinstance(demo, dict):
                run_id = str(demo.get("runId") or "")
            outcome = None
            if ok and run_id and isinstance(payload, dict) and payload.get("await_run"):
                # 同步等待：只占住这一条请求的线程，页面那条上报由另一个线程接
                # （理由写在 `runs.wait_blocking` 的注释里）。
                outcome = runs.wait_blocking(run_id)
                if outcome is None:
                    text = clip(
                        "**没等到沙箱的输出**（页面可能没开着，或者脚本卡住了）。"
                        "这一轮**不要**声称跑出了什么 —— 只说没等到。\n\n" + text
                    )
                else:
                    payload["run"] = outcome
                    text = clip(
                        "沙箱输出（这次工具调用**已经跑完**，这就是结果）：\n"
                        + str(outcome.get("text") or "（没有输出）")
                        + "\n\n" + text
                    )
                # 宿主据此把输出也存进那块零件（前端自己显示的那份只活在当前页面）
                yield {"kind": "run_output", "callId": call_id, "runId": run_id, "run": outcome}

            messages.append({"role": "tool", "tool_call_id": call_id, "content": text})

            # **图交给模型看** —— 他有读图能力（用户："你要保证 matplotlib 的图能被
            # llm 读到——它是有读图能力的"）。原先图只到面板，模型这边干瞪眼，
            # 于是它只能说"我这边看不到图"。
            #
            # 协议上一个细节：`role:"tool"` 只能带文本，图得单独随一条 user 消息走
            # （各家通用做法）。而且**只在模型真能读图时**才发 —— 给读不了图的上游
            # 塞 image_url 会直接 400（`model_reads_images` 就是干这个判断的）。
            #
            # `_fresh` 标记不是给上游的：它让 `_strip_images` 知道"这张是本轮刚拍的，
            # 留一次"，下一轮再发时就换成一句"这里原本有一张图"（base64 重放太贵）。
            images = (outcome or {}).get("images") or []
            if images and gateway.model_reads_images(str(conf.get("model") or "")):
                messages.append(
                    {
                        "role": "user",
                        # `name` 是**合法字段**（上游接受），也是 `_strip_images` 的记号：
                        # 下一轮它就是"看过的那张"，换成一句说明
                        "name": _FIGURE_NAME,
                        "_fresh": True,
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "（上面那次运行的图在下面 —— 你看得到就照着说，"
                                    "看不到就直说看不到。）"
                                ),
                            },
                            *[
                                {
                                    "type": "image_url",
                                    "image_url": {"url": "data:image/png;base64," + str(one)},
                                }
                                for one in images[:3]
                            ],
                        ],
                    }
                )

    # 轮数用尽：这是"模型在打转"，不是上游故障，所以要明确说出来
    yield {
        "kind": "error",
        "message": "工具调用达到上限（" + str(max_turns) + " 轮）",
        "detail": "模型反复要求调用工具，已经停下。把问题问得更具体一些再试。",
        "retryable": False,
    }
    yield {"kind": "finish", "reason": "tool_limit", "usage": usage}
