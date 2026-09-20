"""子代理：把一条**长工具链**派出去，干完只把结果带回来。

## 为什么要有它（以及它照着什么做的）

主对话的上下文是最贵的一条资源：模型在那儿要维持「他是谁、聊到哪了、什么语气」。
而「把这件事查清楚」这类活往往要**十几轮工具调用**（试查询词、换说法、挨个读段落），
那些中间过程对主线毫无用处 —— 塞进去只会把真正要紧的东西挤出去。

所以派活：给子代理一个**任务描述**，它在**自己那套上下文**里干，干完交回一段结果。
主线那边只看到 `tool_start` / `tool_result` 两条事件，中间过程一条都不进。

形状照的是 coding agent 里那种 `Task` 工具（本仓库的开发也是这么用的）：

  * 调用形状是「**任务**」，不是一串工具名 —— 查几次、换什么词、几时收手由它自己定
    （`draft/f.md` 里那条要求：如果调用方是我写死的工具序列，那等于把手动串行
    搬了个地方）；
  * 它有自己的 system（只讲「怎么干活、怎么回报」），不背主对话那套人设；
  * **只回一份结果**：结论 + 依据 + 试过什么 + 没解决的；
  * **不许递归**（它手上没有这个工具），否则轮数预算会失控。

## 权限不放宽

子代理拿的是**主对话当前那一份** `mounts` / `allow`：查询模式（只读）派出去的
子代理也只能读，学习模式派出去的才带着写与执行。它不比主对话多一分权 ——
派出去是为了省上下文，不是为了绕过约束。

## 它不该用来干什么

派出去的是**过程**，不是**判断**。批改、怎么讲、出什么题这类「要说的话」仍由主线
自己说 —— 子代理转述一遍，等于把判断权交了出去（同一个道理见 `draft/f.md`）。
"""

from __future__ import annotations

from . import agent_loop
from . import ai_gateway as gateway

#: 它自己的名字 —— 从子代理的工具集里摘掉（防递归）。
TOOL_NAME = "run_subagent"

#: 子代理**不能用**的工具：
#:
#:   * `run_subagent` —— 防递归（它再派一个，轮数预算就没边了）；
#:   * `run_python` —— 沙箱的代码是**宿主**（页面）拿去执行的：零件发过去、页面跑完
#:     再报回来（见 `app/runs.py`）。子代理的事件**不经过宿主**（它的过程刻意不进
#:     主对话），所以零件根本不会有人去跑 —— 让它带着这个工具只会白等到超时。
#:     要支持得先把子代理的事件转发给宿主，那是另一步的事。
SUBAGENT_DENY: tuple[str, ...] = (TOOL_NAME, "run_python")

#: 子代理最多跑几轮工具调用。它自己决定查几次，所以给得比"够用"宽一点；
#: 超了它会带着已有结果收口（主循环会收到"达到上限"的收尾）。
MAX_TURNS = 8

#: 它交回来的正文上限 —— 这段是要进**主线上下文**、也是要摆到面板上给人看的。
#: **压得比 `parts.MAX_TOOL_OUTPUT`（4000）低**：超过那条线的话，`clip` 会在
#: 我们的说明后面再截一刀，变得两截截断。这里先自己截，并把话说清楚。
REPORT_MAX = 3500


SUBAGENT_SYSTEM = (
    "你是主对话派出来的**子代理**：一个任务落到你手上，你把它做完，只交回结果。\n"
    "\n"
    "## 怎么干\n"
    "* 你手上的工具就是全部家当（和主对话同一批，按这次模式给）；能查就查，"
    "**不要凭记忆编材料名、行号或数值**。\n"
    "* 一件事**多试几次**：一个说法零命中就换词再试（中英、缩写、近义），"
    "别一次不成就在那儿说没有。\n"
    "* **凡引用必附出处**：材料给材料名 + 行号，笔记给库名与标题；"
    "**原文该抄就抄** —— 上级要的常常就是「原话怎么写的」，转述会把关键差别抹掉"
    "（「他认为有误差」和「他说 8e-12 属于舍入积累」不是一回事）。\n"
    "* **冲突两条都报**（笔记写的与材料写的对不上时），**不许替上级调和**、也不许只挑一条。\n"
    "* **失败要说清**：查不到就说查不到，把 `scanned/total` 这类信号原样带上 ——"
    "让上级分得清「真没有」和「词没给对」；试过的查询词一并列出来。\n"
    "\n"
    "## 交回来的样子\n"
    "一份**简短**的回报，四块（Markdown）：\n"
    "1. **结论**：任务问的东西是什么（一两句）。\n"
    "2. **依据**：原文摘录 + 出处（材料名与行号 / 笔记库与标题）。\n"
    "3. **试过什么**：查询词与它的结果，**含零命中的那些**。\n"
    "4. **没解决的**：还有什么没找到、没把握。\n"
    "\n"
    "不要写「我这就去查」这种话，也不要说「让我再看看」—— 直接去查，查完一次交付。\n"
    "**最后一次开口就是交付**：它之前别再念叨（「让我确认一下」「我还想再看看」"
    "这类过渡句一律不要）—— 中间那些自言自语收不到上级那里，只会白占地方。\n"
    "**判断留给上级**：你不判对错、不写评语、不出题、不改任何东西。"
)


def run(  # noqa: ANN001
    db,
    user,
    *,
    task: str,
    wants: str = "",
    mounts: set[str] | None = None,
    allow: tuple[str, ...] | set[str] | None = None,
    tool_context: dict | None = None,
    tools=None,
) -> tuple[bool, dict]:
    """跑一个子代理，返回 `(ok, payload)` —— `payload` 就是给主线的工具结果。

    `mounts` / `allow` 是**主对话当前那一份**（不放权），`tools` 是工具模块本身
    （由调用方传进来，避免 `tools` ↔ `subagent` 互相 import）。
    """
    goal = str(task or "").strip()
    if not goal:
        return False, {"error": "task 不能为空：说清你要它达成什么。"}
    if tools is None:
        return False, {"error": "内部错误：子代理没拿到工具模块。"}

    try:
        conf = gateway.resolve_config(db, user.id)
    except Exception as exc:  # noqa: BLE001 —— 没密钥 / 关了 AI，都如实回一条
        detail = getattr(exc, "detail", None) or str(exc)
        return False, {"error": "派不出去：" + str(detail)}

    system = SUBAGENT_SYSTEM
    extra = str(wants or "").strip()
    if extra:
        system += "\n\n## 上级特别要的东西\n" + extra

    texts: list[str] = []
    used: list[str] = []
    failed = ""
    # 注意：**不开思考**（`thinking` 不传 = 默认关）。子代理的产出是一份给主模型
    # 看的中间结果，思维链不展示给用户 —— 开了只会更慢更贵。要开就在这一处传
    # `thinking=True`（参数已经接好在 `agent_loop.run` 上）。
    for event in agent_loop.run(
        db,
        user,
        conf,
        system=system,
        history=[{"role": "user", "content": goal}],
        tools=tools,
        tool_context=tool_context,
        max_turns=MAX_TURNS,
        mounts=mounts,
        allow=allow,
        # **声明里摘掉、调用也拦住**（见 SUBAGENT_DENY 的说明）
        drop=SUBAGENT_DENY,
    ):
        kind = event.get("kind")
        if kind == "text":
            texts.append(str(event.get("text") or ""))
        elif kind == "tool_start":
            used.append(str(event.get("name") or ""))
            # **新一轮开始：把上一轮攒的文字清掉。**
            #
            # 子代理会边查边念叨（实测："I'll start by locating concepts…"、
            # "I have candidates…"）—— 那些是**过程**，不是交付。它真正要交的只有
            # **收尾那一段**（最后一次开口说的话）。
            #
            # 不清的后果很具体：24 次工具调用的独白全挤进报告，把真正有用的
            # "结论 + 依据"挤出 4000 字的上限（`parts.MAX_TOOL_OUTPUT`），
            # 界面上就只剩一屏过程碎语。用户的原话："这个东西是输入不是结果。"
            texts = []
        elif kind == "error":
            failed = str(event.get("message") or event.get("detail") or "子代理出错了")

    report = "".join(texts).strip()
    if len(report) > REPORT_MAX:
        # 截断要**说清**：不声不响地砍掉半截，上级会以为那就是全部
        report = report[:REPORT_MAX] + "\n\n（回报太长，这里截住了；需要细节就让上级再问一次。）"

    calls = len(used)
    tools_used = sorted(set(name for name in used if name))
    head = "子代理跑完了：用了 " + str(calls) + " 次工具" + (
        "（" + "、".join(tools_used) + "）" if tools_used else ""
    )
    if not report:
        # **失败显式**：没正文就说没正文 —— 不许让它变成一句"材料里没有"的假阴性
        head += "，但**它没交回正文**" + ("（" + failed + "）" if failed else "") + "。"

    payload: dict = {
        "subagent": {
            "calls": calls,
            "used": tools_used,
            "report": report,
            "failed": failed,
        },
        "note": head,
    }
    return True, payload
