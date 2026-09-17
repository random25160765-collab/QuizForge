"""消息的零件（parts）—— 一条消息不是一段文本，而是一串零件。

## 为什么非要零件化

第一版正文是一段字符串，够聊天用；但接下来要装进来的东西都装不下：

* **工具调用**：模型说"我先查一下" —— 这次调用与它的结果必须留在消息里。
  否则用户看到一段没来由的解释，不知道它凭什么这么说。
* **引用**：解释要能点回材料的那几行（`point_sources` 早把行区间存好了）。
* **题卡**：聊着聊着推一道题、就地作答、就地判分，卡就是消息的一部分。
* **推理**：部分模型会返回思考过程，它该可折叠地留着，而不是混进正文。

所以正文（`content`）降级为**投影**：由 `text` 零件拼出来，供列表预览与搜索；
真相在 `parts` 里。这与「题目是事实、索引是投影」是同一条规矩。

## 与 LibreChat 的关系

零件类型照它抄（`packages/data-provider/src/types/content.ts` 的
`text / think / tool_call / summary / error`），另按我们的领域补两类：
`citation`（材料出处）与 `card`（题卡）。还有一处照它做的：
**工具结果不另开消息**，就是 `tool_call` 零件里的 `output` ——
消息数因此不会因为工具调用翻倍（多数实现会为每个工具结果插一条消息）。
"""

from __future__ import annotations

# 单次工具输出留这么多字：一次查询不该把上下文撑爆
MAX_TOOL_OUTPUT = 4000
# 思考过程留这么多：它是过程，不是结论
MAX_THINK = 8000


def clip(text: str, limit: int = MAX_TOOL_OUTPUT) -> str:
    s = str(text or "")
    if len(s) <= limit:
        return s
    return s[:limit] + "\n…（已截断，原文共 " + str(len(s)) + " 字）"


def text_part(text: str) -> dict:
    return {"type": "text", "text": text}


def think_part(text: str) -> dict:
    return {"type": "think", "text": clip(text, MAX_THINK)}


def summary_part(text: str) -> dict:
    """历史被截断处的边界标记（LibreChat 用 LLM 摘要，我们先只留一句话）。"""
    return {"type": "summary", "text": text}


def error_part(message: str, detail: str = "") -> dict:
    return {"type": "error", "message": message, "detail": str(detail or "")[:500]}


def tool_part(
    *,
    name: str,
    args: dict | None = None,
    call_id: str = "",
    output: str = "",
    ok: bool = True,
    ms: int = 0,
) -> dict:
    return {
        "type": "tool_call",
        "id": call_id,
        "name": name,
        "args": args or {},
        "output": clip(output),
        "ok": bool(ok),
        "ms": int(ms or 0),
    }


def citation_part(
    *, slug: str, start_line: int, end_line: int, quote: str = "", point_key: str = ""
) -> dict:
    """材料出处。行区间是它存在的理由 —— 点开就能回到原文那几行。"""
    return {
        "type": "citation",
        "slug": slug,
        "startLine": int(start_line),
        "endLine": int(end_line),
        "quote": str(quote or "")[:300],
        "pointKey": point_key,
    }


def card_part(*, kind: str, payload: dict) -> dict:
    """题卡之类的可交互块。渲染与交互在下一刀，这里先把位置留出来。"""
    return {"type": "card", "kind": kind, "payload": payload or {}}


def draft_part(*, payload: dict) -> dict:
    """临时题卡：模型**现编**的一道题（还没进任何题单）。

    与 `card_part` 分开，是因为两者的"身份"不同：卡片里那道题在题库里
    （判分、答题记录、掌握度都认得它），草稿只在这次对话里 ——
    只有用户按了「存进题单」，它才会拿到一个 `uq-…` 的 id。

    答案随卡片一起走（界面要判分、要讲解）；模型那边由 `tools.output_text`
    裁成一行答案，避免整张卡随历史反复重放。
    """
    return {"type": "draft", "payload": payload or {}}


def action_part(*, kind: str, payload: dict) -> dict:
    """待确认的动作凭条（收藏这道题 / 标成已掌握…）。

    它不是"已经发生的事"，而是一张凭条：AI 提出、界面渲染成可点的控件，
    只有人点了才会真的落到记录里。所以它**不带任何结果字段** ——
    「确认了吗」的答案永远在记录里（记录是事实，界面是投影）：
    界面读记录里那个布尔量就知道该显示"加入"还是"移出"。
    """
    return {"type": "action", "kind": kind, "payload": payload or {}}


def file_part(
    *,
    attachment_id,
    name: str,
    mime: str = "",
    size: int = 0,
    kind: str = "",
    text_chars: int = 0,  # noqa: ANN001
) -> dict:
    """附件零件：**只放元数据**。

    正文（图片二进制、PDF 抽出来的几百 KB 文本）不在这里 —— 零件是每次读会话
    都要发给前端的东西，塞大内容等于让每次翻历史都拖着一个附件走。
    要看内容就按 `attachmentId` 去取（`GET /api/chat/attachments/{id}`）。
    `textChars` 是"抽出来多少字给模型看"，0 表示没抽到（图片就是这样）。
    """
    return {
        "type": "file",
        "attachmentId": str(attachment_id),
        "name": str(name)[:200],
        "mime": str(mime or "")[:120],
        "size": int(size or 0),
        "kind": str(kind or ""),
        "textChars": int(text_chars or 0),
    }


def demo_part(*, title: str, html: str, run_id: str = "") -> dict:
    """演示沙箱：一段 HTML，前端塞进 sandbox iframe 里跑（不带 same-origin）。

    `run_id` 是**页面向宿主回传结果时的身份**：沙箱里跑完（Python 那种）会
    `postMessage({qfRun: run_id, text})`，前端据此把输出认到这条消息上 ——
    没有它，输出就只会飘在面板里，模型永远看不到自己那段代码到底干了什么。
    """
    part = {"type": "demo", "title": str(title)[:80], "html": html}
    if run_id:
        part["runId"] = str(run_id)
    return part


def text_of(parts) -> str:  # noqa: ANN001
    """正文投影：只拼 `text` 零件。

    思考、工具调用、引用不进预览 —— 列表里那一行摘要要的是"它说了什么"。
    """
    chunks = []
    for part in parts or []:
        if isinstance(part, dict) and part.get("type") == "text":
            chunks.append(str(part.get("text") or ""))
    return "\n".join(chunks).strip()


def normalize(raw, fallback_text: str = "") -> list[dict]:  # noqa: ANN001
    """把库里存的东西整成合法的零件数组。

    兼容两类旧数据：只有 `content` 没有 `parts`（第一版的消息）、
    以及 parts 里混进非字典的垃圾。
    """
    out = [
        part
        for part in (raw or [])
        if isinstance(part, dict) and part.get("type")
    ]
    if not out and fallback_text:
        out = [text_part(fallback_text)]
    return out
