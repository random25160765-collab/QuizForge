"""对话 —— 学习前台的会话与消息。

## 这一版有什么、还缺什么

**有了**：消息是零件（`parts`：正文 / 推理 / 工具调用 / 出处 / 题卡位），
模型能调**只读工具**（检索知识空间、取出处、看已有题、看掌握度、看到期复习），
上下文按预算从最新往前截（见 `agent_loop`）。

**还缺**（都是下一刀）：
* **写操作工具**（改掌握度、推题入队）与**题卡** —— 写操作要配"先确认再执行"，
  题卡的渲染与作答要在前端落地（`card` 零件位已经留好）。
* **RAG**：把材料原文按需喂进来。切片一片 1800–3300 token，预算机制就是为了它先建的。
* **附件**：贴一张截图进来问（b.md 里"截一段材料"的实际形态）。

## SSE 事件协议（我们自己的，不是供应商的）

    event: user     data: {...消息}         刚落库的用户消息（前端用它替掉乐观节点）
    event: start    data: {...消息}         助手占位消息（status=streaming）
    event: delta    data: {"text": "..."}   增量文本
    event: usage    data: {...token 数}     上游给了才有
    event: done     data: {...消息}         落库后的最终消息
    event: error    data: {"message","detail","retryable"}

不让前端去解析供应商的格式，换供应商时前端一行都不用改。这与 `routers/ai.py`
「返回体不做二次包装」看似相反，其实是同一类取舍的两个方向：批改是**透传**
（前端早就有解析器了），对话是**自定协议**（前端只解析一次，且必须能被我们改）。

## 为什么中间态要反复落库

一次生成十几秒。用户按了停止、关了页面、网络断了 —— 如果只在结束时写库，
这十几秒的产出就没了，用户看到的是白屏，还会怀疑是自己点错了。
所以：占位行先落库（`streaming`），**收到一块就攒一攒写一次**（`SPILL_SECONDS`），
结束时改成 `ok` / `partial` / `error`，被中断但有产出也照样留下。

## 一个已知的、故意留着没修的边界

客户端断开时，这条路由**感知不到**（同步生成器跑在线程池里，读不到
`request.is_disconnected()`）。后果有两层：

* 已经生成的内容不会丢 —— 靠上面那条「边收边写」，加上读会话时的 `_heal_stale`
  把状态补成 `partial`。
* 但**上游不会立刻停**：供应商还会把话说完，那部分 token 白花了。

要真正省下这笔 token，得把这条路由改成 `async def`，用
`await request.is_disconnected()` 在块与块之间检查、主动关掉上游连接
（同时把库操作挪进 `anyio.to_thread`）。那是一次独立的重构，
而它换来的只是「省钱」，所以先记在这里，不与内核一起做。
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select

from .. import agent_loop, tools
from .. import ai_gateway as gateway
from .. import parts as msgparts
from ..deps import AuthenticatedWriter, CurrentUser, DbSession
from ..models import Conversation, Message

router = APIRouter(prefix="/api/chat", tags=["chat"])

# 人设刻意的短：真正的「懂这个知识空间」应该来自工具与材料，而不是往提示词里堆形容词。
# 这里只定三件事：说什么语言、怎么说话、以及一条硬规矩（不许编出处）。
SYSTEM_PROMPT = (
    "你是 QuizForge 的学习助手，服务一个正在啃 AI 加速器（Tenstorrent / tt-metal）"
    "的学习者。**所有输出一律用中文**（包括调工具前后那半句话），直接讲清机制与因果，"
    "必要时用 Markdown 与 LaTeX，不要空泛鼓励，也不要复述问题。\n"
    "工具有三条检索路：`search_knowledge` 找知识空间里的概念/点，"
    "`search_material` 在材料**原文**里按字面找段落，`explore_graph` 从一个概念往外走关系"
    "（该先学什么、和什么易混）。要查材料、看题库、看他的掌握度时**直接调用**。"
    "说「我去查一下」然后就把话停在那里，等于什么都没做 —— 说完就去查，查完再说结论；"
    "但**查到够回答就收口**，别把回合全花在检索上（最后一轮工具会被收走，你必须开口）。"
    "（如果你确实想先交代一句，那句也必须是中文；任何情况下都不要输出英文句子。）\n"
    "凡引用材料，必须来自工具返回的出处，**不要凭记忆编材料名或行号**；"
    "工具查不到就直说没查到，然后用你自己的理解回答，并说明这部分没有材料支撑。\n"
    "要考他就调 `push_question` 推一张卡 —— **卡片只在你真的调了工具时才会出现**，"
    "在文字里说「我给你推了一道」而他那边什么都没有，是最糟的一种回答。\n"
    "推了题就等他自己作答：不要报答案、不要替他念选项，等他答完再讲。\n"
    "要动他的记录（收藏、标记掌握）就走对应的工具**提案**：界面上会出现一张凭条，"
    "他点了才生效 —— 永远不要声称你已经替他改了记录。"
)

# 消息最长留 8000 字：够贴一段材料，又能挡住把整份材料灌进来的用法
MAX_CONTENT = 8000

# 一条消息最多挂几处引用：再多就成了引文清单，而不是对话
CITATION_MAX = 6

# 回放历史时，单条工具输出最多带这么多字。
# 工具输出会被**每一轮**重新带上，而它往往比对话本身长（一次知识点详情上千字）；
# 实测不夹紧的话，第二轮请求的 prompt 就涨到撞超时。
# 4000 字是"给模型看一次够用"，这里要的是"一直背着也不贵"。
HISTORY_TOOL_CHARS = 700

# 边收边写的间隔：用户停下时，他屏幕上已经出现的字必须已经在库里。
# 值取 1.5 秒 —— 比一次 UPDATE 的成本重要得多的是「停下就留住了」。
SPILL_SECONDS = 1.5

# 超过这么久还停在 streaming 的消息，按「被中断但没来得及收尾」处理。
# 判据用 created_at 而不是 updated_at —— 流式期间我们不写库（省流量），
# 所以 updated_at 停在创建那一刻。代价是：真的连续生成超过 5 分钟会被误判为中断，
# 而这在这个规模上不会发生（真发生了，用户看到的是「已中断 + 已有内容」，不丢东西）。
STALE_STREAM_SECONDS = 300


# ------------------------------------------------------------------ 小工具


def _own_conversation(db: DbSession, user_id, cid: uuid.UUID) -> Conversation:  # noqa: ANN001
    """取出属于这个用户的会话。

    别人的会话按 **404** 处理而不是 403：403 等于承认「这个 id 存在」，
    多租户下不该泄露这种信息。
    """
    conv = db.get(Conversation, cid)
    if conv is None or conv.user_id != user_id:
        raise HTTPException(404, "没有这个对话")
    return conv


def _message_out(m: Message) -> dict:
    return {
        "id": m.id,
        "conversationId": str(m.conversation_id),
        "parentId": m.parent_id,
        "role": m.role,
        "content": m.content,
        "status": m.status,
        "error": m.error,
        "finishReason": m.finish_reason,
        "model": m.model,
        "promptTokens": m.prompt_tokens,
        "completionTokens": m.completion_tokens,
        "latencyMs": m.latency_ms,
        "createdAt": m.created_at.isoformat() if m.created_at else None,
        # 毫秒时间戳与 records.last_at 同一风格：前端的 fmtRelative / fmtTime 直接吃它
        "createdAtMs": int(m.created_at.timestamp() * 1000) if m.created_at else 0,
        # 零件是真相，content 是它的正文投影；旧消息没有 parts，按 content 兜一个
        "parts": msgparts.normalize(m.parts, m.content),
    }


def _conversation_out(c: Conversation, count: int, preview: str) -> dict:
    return {
        "id": str(c.id),
        "title": c.title,
        "messageCount": count,
        "preview": " ".join((preview or "").split())[:80],
        "createdAt": c.created_at.isoformat() if c.created_at else None,
        "updatedAt": c.updated_at.isoformat() if c.updated_at else None,
        "updatedAtMs": int(c.updated_at.timestamp() * 1000) if c.updated_at else 0,
    }


def _sse(event: str, data) -> str:  # noqa: ANN001
    """一条 SSE 记录。

    不加 `id:` 字段：那是给 `Last-Event-ID` 断点续传用的，我们现在不支持 ——
    加上它等于暗示支持。JSON 会把换行转义掉，所以不会破坏 `\\n\\n` 分帧。
    """
    return "event: " + event + "\ndata: " + json.dumps(data, ensure_ascii=False) + "\n\n"


def _title_from(text: str) -> str:
    line = " ".join((text or "").split())[:24]
    return line + ("…" if len(" ".join((text or "").split())) > 24 else "")


def _leaf_id(db: DbSession, conv: Conversation) -> int | None:  # noqa: ANN001
    """这条会话里最新的一条消息 —— 新消息挂在它下面。

    严格说「当前分支的叶节点」才是正确起点（树可能分叉），但最新的一条
    必然属于某条分支的末端，而界面这一版只画一条线，所以等价。
    """
    return db.scalar(
        select(Message.id)
        .where(Message.conversation_id == conv.id, Message.user_id == conv.user_id)
        .order_by(Message.id.desc())
        .limit(1)
    )


def _chain(db: DbSession, node: Message | None) -> list[Message]:  # noqa: ANN001
    """从这条消息沿 `parent_id` 走回根（含自身），返回正序。

    这一个函数同时解决两件事：

    * **界面只画一条线**：库里是树（再生成会分叉），若把所有消息按 id 排出来，
      一次再生成就会显示成两条重复回答；走一遍父亲链正好只拿到当前那条。
    * **上下文从哪儿截**：喂给模型的必须是「到某条消息为止」的那一段。
      再生成时若把当前分支（含上一条回答）当上下文，模型会顺着自己的旧答案
      往下写，而不是重新回答 —— 那就不叫重新生成了。
    """
    chain: list[Message] = []
    seen: set[int] = set()
    while node is not None and node.id not in seen and len(chain) < 500:
        chain.append(node)
        seen.add(node.id)
        node = db.get(Message, node.parent_id) if node.parent_id else None
    chain.reverse()
    return chain


def _active_path(db: DbSession, conv: Conversation) -> list[Message]:  # noqa: ANN001
    """当前分支 = 最新那条消息所在的链。"""
    node = db.scalar(
        select(Message)
        .where(Message.conversation_id == conv.id, Message.user_id == conv.user_id)
        .order_by(Message.id.desc())
        .limit(1)
    )
    return _chain(db, node)


def _heal_stale(db: DbSession, conv: Conversation) -> None:  # noqa: ANN001
    """把「流到一半没人收尾」的消息标成中断。

    正常路径由 `_finish` 收尾；但进程被杀、连接被中间层掐断这些情况下，
    库里会留一条永远的 `streaming`。读的时候顺手治一下，比留一个定时任务简单，
    也比让界面永远转圈诚实。
    """
    from datetime import timedelta

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=STALE_STREAM_SECONDS)
    rows = db.scalars(
        select(Message).where(
            Message.conversation_id == conv.id,
            Message.user_id == conv.user_id,
            Message.status == "streaming",
            Message.created_at < cutoff,
        )
    ).all()
    if not rows:
        return
    for m in rows:
        m.status = "partial" if m.content else "error"
        if not m.content:
            m.error = "生成中断（服务端没有收到收尾信号）"
    db.commit()


def _history(messages: list[Message]) -> list[dict]:
    """把当前分支转成上游要的 messages。

    ## 为什么必须回放工具调用

    这里原先只回放文本（当时记下的一个取舍：工具输出比对话还长，怕上下文膨胀）。
    它的代价实测出来了：模型看不见自己调过什么工具，历史读起来就是
    「我说了句『让我查一下』，然后直接给结论」—— 于是它照着学，
    也先交代一句、然后什么都不做就停下。问「考我一道」有一半次数
    只得到一句英文承诺，就是这么来的。

    所以 assistant 消息要带上 `tool_calls`，紧跟对应的 `role: tool` 结果 ——
    与循环里发给上游的形状完全一样。代价是上下文长一些，
    但预算机制本来就按"从最新往前塞"裁剪，工具输出也各自截断过。

    系统提示**不在这里加**：它由 `agent_loop.build_messages` 统一放进去，
    两边各加一次会让模型收到两条 system 消息（白花 token，还容易被带偏）。
    """
    out: list[dict] = []
    for message in messages:
        if message.role == "user":
            if message.content:
                out.append({"role": "user", "content": message.content})
            continue
        if message.role != "assistant":
            continue

        calls = [
            part
            for part in (message.parts or [])
            if isinstance(part, dict) and part.get("type") == "tool_call" and part.get("name")
        ]
        text = message.content or ""

        if not calls:
            if text:
                out.append({"role": "assistant", "content": text})
            continue

        ids = [
            str(part.get("id") or ("call_" + str(index)))
            for index, part in enumerate(calls)
        ]
        out.append(
            {
                "role": "assistant",
                "content": text,
                "tool_calls": [
                    {
                        "id": ids[index],
                        "type": "function",
                        "function": {
                            "name": part["name"],
                            "arguments": json.dumps(
                                part.get("args") or {}, ensure_ascii=False
                            ),
                        },
                    }
                    for index, part in enumerate(calls)
                ],
            }
        )
        for index, part in enumerate(calls):
            # 回放时把工具输出**夹紧**：它会被每一轮重新带上，4000 字一次的历史
            # 会让第二轮就撞上超时（实测：整份题卡 + 掌握度列表 → 91 秒）。
            # 模型真要那些内容，它会再调一次工具 —— 那比一直背着便宜。
            output = msgparts.clip(str(part.get("output") or ""), HISTORY_TOOL_CHARS)
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": ids[index],
                    "content": output or "（这次调用没有返回内容）",
                }
            )
    return out


# ------------------------------------------------------------------ 会话


@router.get("/conversations")
def list_conversations(user: CurrentUser, db: DbSession) -> dict:
    """会话列表：标题、条数、最后一句话的预览。"""
    rows = db.execute(
        select(Conversation, func.count(Message.id))
        .join(Message, Message.conversation_id == Conversation.id, isouter=True)
        .where(Conversation.user_id == user.id)
        .group_by(Conversation.id)
        .order_by(Conversation.updated_at.desc())
        .limit(200)
    ).all()

    # 每个会话的最后一条消息：DISTINCT ON 一次取回，避免 N+1
    last = {
        cid: content
        for cid, content in db.execute(
            select(Message.conversation_id, Message.content)
            .where(Message.user_id == user.id)
            .distinct(Message.conversation_id)
            .order_by(Message.conversation_id, Message.id.desc())
        ).all()
    }
    return {"conversations": [_conversation_out(c, int(n or 0), last.get(c.id, "")) for c, n in rows]}


@router.get("/search")
def search_messages(
    user: CurrentUser,
    db: DbSession,
    q: str = Query("", description="在消息正文里搜一段字"),
    limit: int = Query(20, ge=1, le=50),
) -> dict:
    """在自己所有对话的消息里搜一段字 —— 「我到底在哪儿学过这个」。

    为什么是 ILIKE 而不是全文索引：中文没有词边界，PG 自带的 FTS 对中文等于没用
    （要装分词扩展），而这里要的恰恰是**子串**命中。消息是千级、一次查回内存，
    ILIKE 够用且零依赖 —— 这也正是 LibreChat 在这个位置上用 MeiliSearch
    而我们不需要它的原因。

    两个字的门槛是刻意的：一个字（"的"、"是"）会命中几乎全部消息，那不是搜索。
    """
    query = " ".join((q or "").split())
    if len(query) < 2:
        return {"items": [], "query": query, "note": "至少输入两个字"}

    rows = db.execute(
        select(Message, Conversation.title)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(Message.user_id == user.id, Message.content.ilike("%" + query + "%"))
        .order_by(Message.id.desc())
        .limit(limit)
    ).all()

    return {
        "items": [
            {
                "messageId": m.id,
                "conversationId": str(m.conversation_id),
                "title": title or "未命名对话",
                "role": m.role,
                "snippet": _snippet(m.content, query),
                "atMs": int(m.created_at.timestamp() * 1000) if m.created_at else 0,
            }
            for m, title in rows
        ],
        "query": query,
    }


def _snippet(content: str, query: str, span: int = 40) -> str:
    """命中处前后各留一点：列表里要能一眼看出"是不是我要找的那句"。"""
    text = " ".join((content or "").split())
    index = text.lower().find(query.lower())
    if index < 0:
        return text[: span * 2] + ("…" if len(text) > span * 2 else "")
    start = max(0, index - span)
    end = min(len(text), index + len(query) + span)
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")


@router.post("/conversations")
def create_conversation(payload: dict, user: AuthenticatedWriter, db: DbSession) -> dict:
    """新建一个空会话。标题留空，等第一条消息自动生成。"""
    title = str((payload or {}).get("title") or "").strip()[:120]
    conv = Conversation(user_id=user.id, title=title)
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return {"conversation": _conversation_out(conv, 0, "")}


@router.get("/conversations/{cid}")
def get_conversation(cid: uuid.UUID, user: CurrentUser, db: DbSession) -> dict:
    """一个会话的**全部消息**（树），当前分支由前端按 `parentId` 算。

    为什么把树整个给出去，而不是只在服务端切好一条线（LibreChat 也是这么做的）：
    「上一个/下一个分支」这个交互需要知道兄弟有哪些、各是哪几条 ——
    每次切换都回服务端问一次，会让翻分支变成一件有延迟的事。
    消息量是千级、一次读回内存，比来回问便宜。
    """
    conv = _own_conversation(db, user.id, cid)
    _heal_stale(db, conv)

    rows = db.scalars(
        select(Message)
        .where(Message.conversation_id == conv.id, Message.user_id == conv.user_id)
        .order_by(Message.id)
    ).all()
    active = _active_path(db, conv)
    return {
        "conversation": _conversation_out(
            conv, len(rows), (active[-1].content if active else "")
        ),
        "messages": [_message_out(m) for m in rows],
        # 服务端算好的当前分支（前端默认用它，之后按用户的切换自己算）
        "activePath": [m.id for m in active],
    }


@router.patch("/conversations/{cid}")
def rename_conversation(cid: uuid.UUID, payload: dict, user: AuthenticatedWriter, db: DbSession) -> dict:
    conv = _own_conversation(db, user.id, cid)
    title = str((payload or {}).get("title") or "").strip()[:120]
    if not title:
        raise HTTPException(400, "标题不能为空")
    conv.title = title
    db.commit()
    return {"ok": True}


@router.delete("/conversations/{cid}")
def delete_conversation(cid: uuid.UUID, user: AuthenticatedWriter, db: DbSession) -> dict:
    """删会话并连带它的消息（外键 CASCADE）。

    这里**不是**「标记退役」—— 那道规矩是给题目与知识点的（历史统计不能有空洞）。
    对话是用户自己的东西，他说删就该真删。
    """
    conv = _own_conversation(db, user.id, cid)
    db.delete(conv)
    db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ 消息（流式）


@router.post("/conversations/{cid}/messages")
def post_message(
    cid: uuid.UUID, payload: dict, user: AuthenticatedWriter, db: DbSession
) -> StreamingResponse:
    """追加一条用户消息，并以 SSE 流式回一条助手消息。

    `replyTo` 给出一条**用户消息**的 id 时，表示「对这条重新生成」——
    不会新增用户消息，只在该消息下再挂一个助手分支（树就长在这里）。

    配置与配额在**开流之前**校验：一旦开始发 SSE，状态码就发出去了，
    那时再报「没填密钥」用户只会看到一个空白的错误。
    """
    conv = _own_conversation(db, user.id, cid)
    body = payload or {}
    content = str(body.get("content") or "").strip()
    reply_to = body.get("replyTo")

    conf = gateway.resolve_config(db, user.id)
    gateway.enforce_quota(db, user.id)

    if reply_to not in (None, "", 0):
        try:
            parent_id = int(reply_to)
        except (TypeError, ValueError):
            raise HTTPException(400, "replyTo 不是合法的消息 id") from None
        parent = db.get(Message, parent_id)
        if parent is None or parent.conversation_id != conv.id or parent.user_id != user.id:
            raise HTTPException(404, "没有这条消息")
        if parent.role != "user":
            raise HTTPException(400, "只能针对用户消息重新生成")
        user_msg = parent
    else:
        if not content:
            raise HTTPException(400, "消息内容不能为空")
        # 新消息挂在谁下面：默认挂最新那条；但前端可以指定 ——
        # 用户翻到旧分支上接着问时，就该挂在那条分支上，而不是挂到最新那条去
        parent_id = _leaf_id(db, conv)
        if "parentId" in body and body["parentId"] is None:
            # **显式**说"没有父节点"。编辑第一条消息时会用到：那一条本来就在根上，
            # 「挂到最新那条下面」是完全不同的意思 —— 不把它与"没给"分开，
            # 编辑第一条消息就会把它挪到会话末尾去。
            parent_id = None
        elif body.get("parentId") not in (None, "", 0):
            try:
                wanted = int(body["parentId"])
            except (TypeError, ValueError):
                raise HTTPException(400, "parentId 不是合法的消息 id") from None
            parent = db.get(Message, wanted)
            if parent is None or parent.conversation_id != conv.id or parent.user_id != user.id:
                raise HTTPException(404, "没有这条消息")
            parent_id = parent.id

        user_msg = Message(
            conversation_id=conv.id,
            user_id=user.id,
            parent_id=parent_id,
            role="user",
            content=content[:MAX_CONTENT],
            status="ok",
        )
        db.add(user_msg)
        if not conv.title.strip():
            conv.title = _title_from(content)
        conv.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(user_msg)

    # 上下文 = 到这条提问为止的那一段（不含任何别的分支上的回答）
    history = _history(_chain(db, user_msg))

    assistant = Message(
        conversation_id=conv.id,
        user_id=user.id,
        parent_id=user_msg.id,
        role="assistant",
        content="",
        status="streaming",
        model=conf["model"],
    )
    db.add(assistant)
    db.commit()
    db.refresh(assistant)

    return StreamingResponse(
        _stream(db, user, conv, conf, user_msg, assistant, history),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # nginx 默认会攒够一个缓冲区才往下发 —— 那会把流式体验直接抵消掉
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/conversations/{cid}/messages/{mid}/stop")
def stop_message(cid: uuid.UUID, mid: int, user: AuthenticatedWriter, db: DbSession) -> dict:
    """前端按了停止 —— 把这条正在生成的消息收尾成「已中断」。

    为什么要专门开一个端点：服务端**自己感知不到**客户端断开（见模块 docstring），
    没有这一步的话，那条消息会停在 `streaming`，直到下次读会话被 `_heal_stale`
    补上（五分钟之后）—— 中间这段时间库里的事实与用户刚做的事不一致。

    `_finish` 会尊重这次收尾，不再覆盖（否则"停止"过一会儿会自己变回 `ok`，
    用户下次打开看到一条他没读完却被标成完整的回答）。
    """
    conv = _own_conversation(db, user.id, cid)
    m = db.get(Message, mid)
    if m is None or m.conversation_id != conv.id or m.user_id != user.id:
        raise HTTPException(404, "没有这条消息")

    if m.status == "streaming":
        m.status = "partial" if m.content else "error"
        if not m.content:
            m.error = "已被中断"
        conv.updated_at = datetime.now(timezone.utc)
        db.commit()
    return {"ok": True, "status": m.status}


def _push_text(parts: list[dict], chunk: str) -> None:
    """把一段增量并进零件数组。

    同类相邻就合并 —— 否则一次回答会碎成几百个 `text` 零件，
    存库、传输、渲染都白受一遍。
    """
    if parts and parts[-1].get("type") == "text":
        parts[-1]["text"] = str(parts[-1].get("text") or "") + chunk
    else:
        parts.append(msgparts.text_part(chunk))


def _push_think(parts: list[dict], chunk: str) -> None:
    if parts and parts[-1].get("type") == "think":
        parts[-1]["text"] = str(parts[-1].get("text") or "") + chunk
    else:
        parts.append(msgparts.think_part(chunk))


def _spill(db: DbSession, assistant: Message, parts: list[dict]) -> None:  # noqa: ANN001
    """把已经收到的零件先写进库（见模块 docstring 的「边收边写」）。

    只改内容，不动状态：状态由 `_finish` 或读会话时的 `_heal_stale` 负责。
    这样即使进程被杀，库里也留着「一部分内容 + streaming」——
    读的时候它会自己变成 `partial`，而不是白屏。
    """
    assistant.parts = list(parts)
    assistant.content = msgparts.text_of(parts)
    db.commit()


def _finish(  # noqa: ANN001
    db: DbSession,
    user,
    conv: Conversation,
    assistant: Message,
    parts: list[dict],
    status: str,
    error: str,
    finish: str,
    latency_ms: int,
    usage: dict,
) -> None:
    """落库 + 记账。正常、出错、被中断三条路都走它。"""
    db.refresh(assistant)
    closed = assistant.status in ("partial", "error")
    if not closed:
        # 没被别人收尾（比如前端点了停止调了 stop 端点）才写内容与状态；
        # 但用量照样记 —— 那部分 token 是真的花掉了
        assistant.parts = list(parts)
        assistant.content = msgparts.text_of(parts)
        assistant.status = status
        assistant.error = error[:2000]
    assistant.finish_reason = finish
    assistant.latency_ms = latency_ms
    assistant.prompt_tokens = int(usage.get("promptTokens") or 0)
    assistant.completion_tokens = int(usage.get("completionTokens") or 0)
    conv.updated_at = datetime.now(timezone.utc)
    db.commit()

    gateway.record_usage(
        db,
        user,
        ok=(status == "ok"),
        latency_ms=latency_ms,
        prompt_tokens=assistant.prompt_tokens,
        completion_tokens=assistant.completion_tokens,
        detail=error,
    )


def _stream(  # noqa: ANN001
    db: DbSession,
    user,
    conv: Conversation,
    conf: dict,
    user_msg: Message,
    assistant: Message,
    history: list[dict],
):
    started = time.perf_counter()
    parts: list[dict] = []
    tool_parts: dict[str, dict] = {}  # callId → 零件（结果回来时原地补上）
    citations: list[dict] = []  # 这条消息已经挂上去的引用
    cited: set[tuple] = set()  # (材料, 起, 止)，用来去重
    usage = {"promptTokens": 0, "completionTokens": 0}
    finish = ""
    status = "ok"
    error = ""
    error_sent = False
    last_spill = started

    yield _sse("user", _message_out(user_msg))
    yield _sse("start", _message_out(assistant))

    try:
        for event in agent_loop.run(
            db,
            user,
            conf,
            system=SYSTEM_PROMPT,
            history=history,
            tools=tools,
            # 工具要知道"这是哪次对话"：push_question 靠它避开这次已经推过的题
            tool_context={"conversationId": str(conv.id)},
        ):
            kind = event["kind"]

            if kind == "text":
                _push_text(parts, event["text"])
                yield _sse("delta", {"text": event["text"]})
            elif kind == "think":
                _push_think(parts, event["text"])
                yield _sse("think", {"text": event["text"]})
            elif kind == "note":
                yield _sse("note", {"text": event["text"]})
            elif kind == "tool_start":
                part = msgparts.tool_part(
                    name=event["name"], args=event["args"], call_id=event["callId"]
                )
                parts.append(part)
                tool_parts[event["callId"]] = part
                yield _sse(
                    "tool",
                    {
                        "phase": "start",
                        "callId": event["callId"],
                        "name": event["name"],
                        "args": event["args"],
                    },
                )
            elif kind == "tool_result":
                part = tool_parts.get(event["callId"])
                if part is not None:
                    part["output"] = event["output"]
                    part["ok"] = event["ok"]
                    part["ms"] = event["ms"]
                yield _sse(
                    "tool",
                    {
                        "phase": "result",
                        "callId": event["callId"],
                        "name": event["name"],
                        "ok": event["ok"],
                        "ms": event["ms"],
                        "output": event["output"],
                    },
                )

                # 有的工具结果不只给模型看，还要变成零件：题卡就是 ——
                # 模型看到题面好围绕它说话，界面把它渲染成**可作答的卡片**。
                # 一条消息因此能"长出"一道题，而不是只有一段文字。
                card = (event.get("payload") or {}).get("card")
                if isinstance(card, dict) and card.get("questionId"):
                    parts.append(msgparts.card_part(kind="question", payload=card))
                    yield _sse("card", {"callId": event["callId"], "card": card})

                # 写操作也一样：工具只**提案**，界面上长出一张待确认的凭条，
                # 他点了才落到记录里（走的是收藏夹 / 错题本那两条老路）。
                proposal = (event.get("payload") or {}).get("proposal")
                if isinstance(proposal, dict) and proposal.get("kind"):
                    parts.append(
                        msgparts.action_part(kind=proposal["kind"], payload=proposal)
                    )
                    yield _sse("action", {"callId": event["callId"], "proposal": proposal})

                # 出处 → 引用零件：任何工具只要在结果里带 `sources`，这一处就统一
                # 把它变成可点回原文的引用。所以"有没有引用"不取决于工具，
                # 而取决于它有没有给出处（这是流水线早就立下的规矩）。
                for source in event.get("payload", {}).get("sources") or []:
                    if len(citations) >= CITATION_MAX:
                        break
                    key = (
                        str(source.get("material") or ""),
                        int(source.get("startLine") or 0),
                        int(source.get("endLine") or 0),
                    )
                    if not key[0] or key in cited:
                        continue
                    cited.add(key)
                    citation = msgparts.citation_part(
                        slug=key[0],
                        start_line=key[1],
                        end_line=key[2],
                        quote=str(source.get("quote") or ""),
                        point_key=str(source.get("pointKey") or ""),
                    )
                    citations.append(citation)
                    parts.append(citation)
                    yield _sse("citation", {"callId": event["callId"], "citation": citation})
            elif kind == "error":
                status = "error"
                error = str(event.get("detail") or event.get("message") or "生成失败")
                error_sent = True
                yield _sse(
                    "error",
                    {
                        "message": event.get("message") or "生成失败",
                        "detail": event.get("detail") or "",
                        "retryable": bool(event.get("retryable")),
                    },
                )
            elif kind == "finish":
                finish = str(event.get("reason") or "")
                usage = event.get("usage") or usage

            now = time.perf_counter()
            if now - last_spill >= SPILL_SECONDS:
                last_spill = now
                _spill(db, assistant, parts)
    except GeneratorExit:
        # 客户端走了（点停止 / 关页面）。产出不能丢，但这里**不能再 yield** ——
        # 生成器正在关闭，往一条已死的连接写东西只会抛 RuntimeError。
        produced = bool(msgparts.text_of(parts)) or any(
            part.get("type") == "tool_call" for part in parts
        )
        _finish(
            db,
            user,
            conv,
            assistant,
            parts,
            "partial" if produced else "error",
            "" if produced else "已被中断",
            finish,
            gateway.elapsed_ms(started),
            usage,
        )
        raise
    except Exception as exc:  # noqa: BLE001
        # 状态码早就发出去了，这里能做的就是变成一条 error 事件，
        # 而不是让连接就这么断在半路（前端收不到收尾事件会一直转圈）
        status = "error"
        error = str(exc)[:300]

    _finish(db, user, conv, assistant, parts, status, error, finish, gateway.elapsed_ms(started), usage)

    if status == "error":
        if not error_sent:
            yield _sse("error", {"message": "生成失败", "detail": error, "retryable": True})
        return

    yield _sse("usage", usage)
    yield _sse("done", _message_out(assistant))
