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
#:
#: **压得比 `parts.MAX_TOOL_OUTPUT` 低**：那份是工具结果的总闸（20000 字），
#: 超过它 `clip` 还会在我们这句说明后面再截一刀，变成两截截断 —— 读的人分不清
#: 哪一刀是谁切的。这里先自己截，并把话说清楚。
#:
#: 2026-09-26 从 3500 一路提到 18000：实测一份"清单表 + 四块证据"的回报正好在 3500 处
#: 被截在第四块中间，而**表头那一行**（`| slug | 标题 | 年份 |`…）才是上级真正要的
#: 东西 —— 它恰恰最占地方，也就最容易被挤掉。用户看过之后要求放大到 20000；
#: 这里取 **18000** 而不是 20000，留 2000 字给外层的信封与我们的说明 ——
#: 因为 `parts.MAX_TOOL_OUTPUT` 也是 20000，而它 `clip` 的是**整个工具结果的文本**：
#: 一模一样的话，被切掉的正好是**末尾那句"截到哪儿了、还剩多少"** ——
#: 那等于把唯一的解释也吃掉（这个坑与"两道闸叠在一起"是同一件事）。
REPORT_MAX = 18000

#: 短于这个长度、**又是在说"接下来要做什么"**的，就不算一份回报（见 `_looks_like_intent`）。
#:
#: 为什么需要它：子代理干活期间会**边查边念叨**，而它真正交的只有最后一次开口。
#: 于是当它还没收口就用完轮数时，"最后一次开口"就是半句**打算** —— 实测七次调用里
#: 有三次如此，报回来的是 "Excellent — … Let me read it in different regions …"、
#: "I have read all the target notes … Now let me verify …"，而主线会把它当成任务结果。
REPORT_MIN = 700

#: "这是在说接下来要做什么"，不是"这是我查到的"。中英都收 —— 实测子代理的**工作独白**
#: 是英文、**交付**是中文，但这条不靠语言判断（那份 975 字的真回报也是英文的）。
_INTENT_MARKS = (
    "let me ",
    "let's ",
    "now let me",
    "i'll ",
    "i will ",
    "i have read",
    "让我",
    "接下来我",
    "我先去",
    "我这就",
)

#: 最后一轮工具收回去了 —— 跟它明说一句。见 `agent_loop.run` 用它的那处。
FINAL_NUDGE = (
    "（工具已经收回，这一轮不能再调用了。）现在**交付**：按上面那四块把结果写出来 ——"
    "结论、依据（带出处与原文摘录）、试过什么（含零命中的）、还有什么没解决。"
    "**不要再写「让我再看看」这类过渡句** —— 你这次开口就是交给上级的东西。"
)


def _looks_like_intent(text: str) -> bool:
    """这段是"打算去做"的念叨，而不是"查到了什么"的交付？

    只在**短文本**上判：一份长回报里出现一次 "let me" 不算什么（实测那份 975 字的
    真回报就是英文写的，不能因为语言或某个词就把它判下去）。

    判错了也不致命 —— 正文照样带回去，只是**换了名字**（`report` → `lastWords`）
    并加一句说明，让主线知道该再派一次或自己查，而不是拿着半句念叨当结论。
    """
    if len(text) >= REPORT_MIN:
        return False
    low = text.lower()
    return any(mark in low for mark in _INTENT_MARKS)


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
    "**写之前先算好这块地方：回报上限 " + str(REPORT_MAX) + " 字，超了就在换行处"
    "被截掉、上级再也看不到。** 所以顺序是：先把 **1 与 4** 写完整（那是上级最要的），"
    "再放 2 的证据 —— 证据挑**最关键的几条原文**，别把读过的都抄一遍；"
    "表格比正文更占地方，列要克制（问什么列什么）。真的写不完，就在末尾写明"
    "「第 N 部分到此，后面还有 X」，让上级按部分再派一次。\n"
    "\n"
    "不要写「我这就去查」这种话，也不要说「让我再看看」—— 直接去查，查完一次交付。\n"
    "**最后一次开口就是交付**：它之前别再念叨（「让我确认一下」「我还想再看看」"
    "这类过渡句一律不要）—— 中间那些自言自语收不到上级那里，只会白占地方。\n"
    "**判断留给上级**：你不判对错、不写评语、不出题、不改任何东西。"
)


def run(  # noqa: ANN001
    db,
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
        conf = gateway.resolve_config(db)
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
        # 最后一轮工具会被收回，而模型察觉不到 —— 明说一句，好让它交**结论**
        # 而不是半句"让我再看看"（见 agent_loop 里用它的那处）
        final_nudge=FINAL_NUDGE,
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
            # "结论 + 依据"挤出上限（`REPORT_MAX`），界面上就只剩一屏过程碎语。
            # 用户的原话："这个东西是输入不是结果。"
            texts = []
        elif kind == "error":
            failed = str(event.get("message") or event.get("detail") or "子代理出错了")

    report = "".join(texts).strip()
    if len(report) > REPORT_MAX:
        # 截断要**说清**，而且要切在**换行处**：切在半句话上，读的人会以为正文
        # 到那儿就完了（实测："…未扫的 20 份被列在 `skipped` 字段，且" 后面
        # 直接接一句说明，看起来像原文写了一半）。
        cut = report.rfind("\n", 0, REPORT_MAX)
        if cut < REPORT_MAX // 2:
            cut = REPORT_MAX  # 整段就是一大行（实测真有，2 行 3500 字）：退化成硬截
        kept = report[:cut]
        # **丢了多少字要说出来**，并给一条具体可执行的下一步：只说"需要细节再问一次"
        # 等于没说 —— 上级既不知道丢了什么、也不知道该怎么问（实测就是这么被卡的）。
        report = kept + (
            "\n\n（回报太长，在换行处截住了：它交上来 %d 字，这里留了 %d 字，"
            "**后面还有约 %d 字没交**。要那部分就照它自己标的分节再派一次，"
            "任务里写明「只交第 N 部分」，别让它整篇重写。）"
            % (len(report), len(kept), len(report) - len(kept))
        )

    # **交付了没有**：短、而且是在说"接下来要做什么" —— 那是干活时的念叨，不是回报。
    delivered = bool(report) and not _looks_like_intent(report)

    calls = len(used)
    tools_used = sorted(set(name for name in used if name))
    head = "子代理跑完了：用了 " + str(calls) + " 次工具" + (
        "（" + "、".join(tools_used) + "）" if tools_used else ""
    )
    if not report:
        # **失败显式**：没正文就说没正文 —— 不许让它变成一句"材料里没有"的假阴性
        head += "，但**它没交回正文**" + ("（" + failed + "）" if failed else "") + "。"
    elif not delivered:
        head += (
            "，但它**没交回回报**（只留了半句还在干活的话）—— "
            "要结论就换个更具体的任务再派一次，或者自己查。"
        )

    payload: dict = {
        "subagent": {
            "calls": calls,
            "used": tools_used,
            # 没交付时 `report` **留空**：这个名字就是"交付"，名不副实比空着更糟
            "report": report if delivered else "",
            "delivered": delivered,
            # 它最后那句话**换个名字**带上：有时能看出它卡在哪（"Let me read it in
            # different regions…"说明它还没读完），但它不配叫 report —— 名字就是承诺。
            "lastWords": "" if delivered else report,
            "failed": failed,
        },
        "note": head,
    }
    return True, payload
