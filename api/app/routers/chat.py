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

import base64
import json
import time
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm.attributes import flag_modified

from .. import agent_loop, tools
from .. import ai_gateway as gateway
from .. import attachments as attach
from .. import parts as msgparts
from .. import mounts
from ..db import as_json
from ..deps import AuthenticatedWriter, CurrentUser, DbSession
from ..models import Attachment, Conversation, ConversationFolder, Message

router = APIRouter(prefix="/api/chat", tags=["chat"])


@router.get("/mounts")
def mounts_state(user: CurrentUser, db: DbSession) -> dict:
    """顶栏那组开关现在的状态：五组各亮着没有，以及**这一版真声明了哪些工具**。

    `declared` 是从 `tools.specs()` 真算出来的，不是另抄一份 ——
    "图标亮着"与"模型看得到几个工具"必须能对得上，而这是唯一能证明它俩一致的办法
    （改完在界面上量一下 `declared` 的条数，就知道开关有没有真的接上）。
    """
    return mounts.describe(db, user.id)


@router.post("/mounts")
def mounts_save(body: dict, user: CurrentUser, db: DbSession) -> dict:
    """存一份挂载集。**空列表是合法的** —— 那就是极简模式（一条工具都不声明）。"""
    groups = body.get("groups")
    if not isinstance(groups, list):
        raise HTTPException(status_code=400, detail="groups 得是个列表")
    try:
        mounts.write(db, user.id, [str(part) for part in groups])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return mounts.describe(db, user.id)

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
    "他要「跑一段脚本 / 算一下 / 验证某个算法」时**直接动手**：`run_python` 能在沙箱里"
    "真跑 Python；机制里有空间或时间结构时，`render_demo` 画一个能动的演示"
    "（沙箱里已经备好 React + JSX + d3 与一套现成组件，见该工具说明里的骨架 —— "
    "**别自己引库、也别手画坐标轴**）。"
    "不要因为「我做不到」就推辞，也不要把「做不到」当结论 —— 先看看手上的工具能做到哪一步。\n"
    "要动他的记录（收藏、标记掌握）就走对应的工具**提案**：界面上会出现一张凭条，"
    "他点了才生效 —— 永远不要声称你已经替他改了记录。"
)

# 消息最长留 8000 字：够贴一段材料，又能挡住把整份材料灌进来的用法
MAX_CONTENT = 8000

# 一条消息最多挂几处引用：再多就成了引文清单，而不是对话
CITATION_MAX = 6

# 图片发给模型时的两道闸：单张上限与每轮张数上限。
# base64 之后体积要涨三分之一，而这图会进每一次请求 —— 一张 4MB 的照片
# 换来的上下文占用，比它带来的信息值钱得多。
VISION_MAX_BYTES = 4 * 1024 * 1024
VISION_MAX_IMAGES = 3

# 沙箱输出存下来时留多少字（回放给模型时另按 HISTORY_RUN_CHARS 夹一次）
RUN_TEXT_LIMIT = 40_000
# 回放沙箱输出给模型时留多少字。它会被每一轮重新带上，所以要比"存下来的"更狠
HISTORY_RUN_CHARS = 1600

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
        # 这一轮挂载了哪几组工具（老消息是空列表：那时还没记这一项）
        "mounts": [str(one) for one in as_json(m.mounts, []) or []],
        "promptTokens": m.prompt_tokens,
        "completionTokens": m.completion_tokens,
        "latencyMs": m.latency_ms,
        "createdAt": m.created_at.isoformat() if m.created_at else None,
        # 毫秒时间戳与 records.last_at 同一风格：前端的 fmtRelative / fmtTime 直接吃它
        "createdAtMs": int(m.created_at.timestamp() * 1000) if m.created_at else 0,
        # 零件是真相，content 是它的正文投影；旧消息没有 parts，按 content 兜一个
        "parts": msgparts.normalize(m.parts, m.content),
    }


def _ensure_folder(db, user_id, path: str) -> None:  # noqa: ANN001
    """保证这个分组**及其各级父分组**都登记在册（幂等；不提交）。"""
    if not path:
        return
    parts = path.split("/")
    for i in range(1, len(parts) + 1):
        one = "/".join(parts[:i])
        exists = db.execute(
            select(func.count())
            .select_from(ConversationFolder)
            .where(ConversationFolder.user_id == user_id, ConversationFolder.path == one)
        ).scalar()
        if not exists:
            db.add(ConversationFolder(user_id=user_id, path=one))


def _conversation_out(c: Conversation, count: int, preview: str) -> dict:
    return {
        "id": str(c.id),
        "title": c.title,
        "messageCount": count,
        "preview": " ".join((preview or "").split())[:80],
        "pinned": bool(c.pinned),
        # 所属分组（路径，`''` = 根）。前端拿它把会话摆进目录树里
        "folder": c.folder or "",
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


def _attachment_rows(db: DbSession, message: Message) -> list:  # noqa: ANN001
    """这条消息挂的附件行（按零件顺序）。"""
    ids = []
    for part in message.parts or []:
        if isinstance(part, dict) and part.get("type") == "file":
            try:
                ids.append(uuid.UUID(str(part.get("attachmentId"))))
            except (TypeError, ValueError):
                continue
    if not ids:
        return []
    rows = db.scalars(select(Attachment).where(Attachment.id.in_(ids))).all()
    order = {str(value): index for index, value in enumerate(ids)}
    return sorted(rows, key=lambda row: order.get(str(row.id), 0))


def _attachment_images(db: DbSession, message: Message) -> list[dict]:  # noqa: ANN001
    """图片附件 → 上游能吃的图像块（data URL）。

    只在模型能读图时调用（见 `ai_gateway.model_reads_images`）。
    单张限 `VISION_MAX_BYTES`、最多 `VISION_MAX_IMAGES` 张：base64 之后体积还要涨三分之一，
    而这张图会进每一次请求的上下文。
    """
    blocks = []
    for row in _attachment_rows(db, message):
        if row.kind != "image" or (row.size or 0) > VISION_MAX_BYTES:
            continue
        if len(blocks) >= VISION_MAX_IMAGES:
            break
        mime = str(row.mime or "").strip()
        if mime not in gateway.VISION_MIMES:
            # bmp / svg 存得下，但上游只收 png/jpeg/webp/gif（多传会 400）。
            # 不静默丢掉：说明里会讲清是哪张、为什么没给出去。
            continue
        try:
            raw = attach.path_of(row).read_bytes()
        except OSError:
            continue
        blocks.append(
            {
                "type": "image_url",
                "image_url": {"url": "data:" + mime + ";base64," + base64.b64encode(raw).decode("ascii")},
            }
        )
    return blocks


def _attachment_block(db: DbSession, message: Message, *, model: str = "", vision: bool = False) -> str:  # noqa: ANN001
    """把这条消息挂的附件正文拼成一段，附在**发给模型的那一份**后面。

    刻意不写进库里那条 `content`：那是用户说的话，规矩是"原样留着他说的"。
    抽不到正文的（图片）也要说清是怎么回事 —— 但**别只说"读不到"**：

    * 模型能读图（`vision`）→ 图像已经随消息发过去了，这里只留一行"有图"的说明
    * 读不了 → 说清是**哪个模型**读不了、以及能怎么办（抄文字 / 换个能读图的模型）。
      原先只说"读不到"，用户看到的是一句无解的话（实测他就是这么问回来的）。
    """
    rows = _attachment_rows(db, message)
    if not rows:
        return ""

    blocks = []
    for row in rows:
        head = f"【附件：{row.name}（{row.size // 1024}KB）】"
        if row.text:
            blocks.append(head + "\n" + row.text[: attach.CONTEXT_LIMIT])
        elif row.kind == "image":
            mime = str(row.mime or "").strip()
            if vision and mime in gateway.VISION_MIMES:
                blocks.append(head + "\n（这是一张图片，图像已随这条消息交给你，可以直接看图回答。）")
            elif vision:
                blocks.append(
                    head
                    + "\n（这是一张图片，格式是 "
                    + (mime or "未知")
                    + "：上游只接受 png / jpeg / webp / gif，所以这张没能交给你。"
                    "跟他说一声，让他转成 png 或 jpg 再发。）"
                )
            else:
                blocks.append(
                    head
                    + "\n（这是一张图片。当前模型"
                    + (f"（{model}）" if model else "")
                    + "读不到图像内容：你只知道他贴了一张图，看不到里面是什么。"
                    "别猜图里有什么 —— 直接说你需要他把关键内容抄成文字，"
                    "或者让他换一个能读图的模型。）"
                )
        else:
            blocks.append(head + "\n（没能从里面抽出文本。）")
    return "\n\n".join(blocks)


def _history(db: DbSession, messages: list[Message], *, vision: bool = False, model: str = "") -> list[dict]:  # noqa: ANN001
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

    ## 图片只在"最新那条"上带

    `vision` 为真（模型能读图）时，用户消息里的图片会作为图像发出去。
    但**只带最后一条**：图像很占上下文，历史里每轮重发一次既贵又没意义 ——
    上一轮的图，上一轮已经讲过了。

    系统提示**不在这里加**：它由 `agent_loop.build_messages` 统一放进去，
    两边各加一次会让模型收到两条 system 消息（白花 token，还容易被带偏）。
    """
    out: list[dict] = []
    last_user_id = messages[-1].id if messages else None
    for message in messages:
        if message.role == "user":
            text = message.content or ""
            extra = _attachment_block(db, message, model=model, vision=vision)
            if extra:
                text = (text + "\n\n" + extra).strip()
            images = (
                _attachment_images(db, message)
                if vision and message.id == last_user_id
                else []
            )
            if images:
                # OpenAI 兼容的多模态形状：文本块 + 图像块
                content = [{"type": "text", "text": text or "（见附件）"}] + images
                out.append({"role": "user", "content": content})
            elif text:
                out.append({"role": "user", "content": text})
            continue
        if message.role != "assistant":
            continue

        calls = [
            part
            for part in (message.parts or [])
            if isinstance(part, dict) and part.get("type") == "tool_call" and part.get("name")
        ]
        text = message.content or ""

        # 沙箱跑出来的输出也回放进来：模型于是能读到自己那段代码的结果。
        # 原先它读不到 —— 只能靠用户在面板上按一下「把输出发给它」再转述，
        # 那是把用户当传话筒。输出由前端回填（见 attach_run），这里只是把它带上。
        runs = [
            str((part.get("run") or {}).get("text") or "").strip()
            for part in (message.parts or [])
            if isinstance(part, dict)
            and part.get("type") == "demo"
            and isinstance(part.get("run"), dict)
        ]
        for output in runs:
            if output:
                text = (text + "\n\n〔沙箱输出〕\n" + msgparts.clip(output, HISTORY_RUN_CHARS)).strip()

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
    """会话列表：标题、条数、最后一句话的预览。

    排序 = **置顶的在前**，各自按最近活动倒序。置顶是用户自己钉的
    （"这条我在攻"），所以它必须压过"谁最近动过"。
    """
    rows = db.execute(
        select(Conversation, func.count(Message.id))
        .join(Message, Message.conversation_id == Conversation.id, isouter=True)
        .where(Conversation.user_id == user.id)
        .group_by(Conversation.id)
        .order_by(Conversation.pinned.desc(), Conversation.updated_at.desc())
        .limit(200)
    ).all()

    # 每个会话的最后一条消息：一次取回，避免 N+1。
    #
    # 用**窗口函数**而不是 `DISTINCT ON`：后者是 Postgres 专有，SQLite 上会被
    # **静默忽略** —— 那比报错更糟（列表里的预览会变成随便哪一条）。
    ranked = (
        select(
            Message.conversation_id.label("cid"),
            Message.content.label("content"),
            func.row_number()
            .over(partition_by=Message.conversation_id, order_by=Message.id.desc())
            .label("rank"),
        )
        .where(Message.user_id == user.id)
        .subquery()
    )
    last = {
        row.cid: row.content
        for row in db.execute(select(ranked.c.cid, ranked.c.content).where(ranked.c.rank == 1))
    }
    # 分组：表里那些（含**空**分组）+ 会话自己带的那些（老数据可能没登记过）。
    # 一次请求全给出去：左栏那棵树要"目录 + 会话"一起才画得出来。
    names = {
        str(row[0])
        for row in db.execute(select(ConversationFolder.path).where(ConversationFolder.user_id == user.id))
    }
    for c, _n in rows:
        if c.folder:
            names.add(c.folder)
            # 父级也要在：`a/b` 存在就说明 `a` 也是分组（可能是搬迁留下来的）
            parts = c.folder.split("/")
            for i in range(1, len(parts)):
                names.add("/".join(parts[:i]))

    return {
        "conversations": [_conversation_out(c, int(n or 0), last.get(c.id, "")) for c, n in rows],
        "folders": sorted(names),
    }


# ------------------------------------------------------------------ 分组


MAX_FOLDER_LEN = 240


def _clean_folder(raw) -> str:  # noqa: ANN001
    """把用户给的名字/路径整理成一个干净的路径：去掉空段与 `.`/`..`，限长。

    `.` 与 `..` 必须去掉 —— 在这一层它们只是**字符串**（不是真文件系统），
    留着只会在界面上长出一个叫 `..` 的目录，谁也说不清它是什么意思。
    """
    text = str(raw or "").replace("\\", "/")
    parts = [p.strip() for p in text.split("/")]
    return "/".join(p for p in parts if p and p not in (".", ".."))[:MAX_FOLDER_LEN]


def _folder_rows(db, user_id, path: str):  # noqa: ANN001, ANN202
    """这个分组**及其子树**（按路径前缀认子树）。"""
    return (
        db.execute(
            select(ConversationFolder).where(
                ConversationFolder.user_id == user_id,
                or_(ConversationFolder.path == path, ConversationFolder.path.like(path + "/%")),
            )
        )
        .scalars()
        .all()
    )


@router.post("/folders")
def create_folder(payload: dict, user: AuthenticatedWriter, db: DbSession) -> dict:
    """新建一个分组（连同它的各级父分组）。**幂等**：同名再点一次不报错。

    父级一起建：`a/b` 建出来而 `a` 不在的话，树里会凭空多出一层没有名字的中间层。
    """
    path = _clean_folder((payload or {}).get("path"))
    if not path:
        raise HTTPException(400, "分组名不能为空")
    _ensure_folder(db, user.id, path)
    db.commit()
    return {"ok": True, "path": path}


@router.patch("/folders")
def rename_folder(payload: dict, user: AuthenticatedWriter, db: DbSession) -> dict:
    """改名 / 搬家：整个子树一起走（分组记录 + 里面的会话）。

    目标已存在时**拒绝**，不合并：两个分组悄悄并成一个，用户回头会找不到东西。
    """
    body = payload or {}
    old = _clean_folder(body.get("path"))
    new = _clean_folder(body.get("to"))
    if not old or not new:
        raise HTTPException(400, "分组名不能为空")
    if new == old:
        return {"ok": True, "path": new}
    if new.startswith(old + "/"):
        raise HTTPException(400, "不能把分组挪进它自己里")
    exists = db.execute(
        select(func.count())
        .select_from(ConversationFolder)
        .where(ConversationFolder.user_id == user.id, ConversationFolder.path == new)
    ).scalar()
    if exists:
        raise HTTPException(409, "已经有这个分组了")

    # 前缀替换：`old` 与 `old/...` 一律换成 `new` 开头（会话与分组记录两边都要）
    for conv in (
        db.execute(
            select(Conversation).where(
                Conversation.user_id == user.id,
                or_(Conversation.folder == old, Conversation.folder.like(old + "/%")),
            )
        )
        .scalars()
        .all()
    ):
        conv.folder = new + (conv.folder or "")[len(old):]
    for folder in _folder_rows(db, user.id, old):
        folder.path = new + folder.path[len(old):]
    # 先把上面的改名**落库**，再问"新名字在不在"。
    # 踩过：会话是 `autoflush=False`，不 flush 的话 `_ensure_folder` 数的还是改名前的
    # 库（数到 0）→ 又插一条新名字 → 提交时撞 UNIQUE（子树自己已经占着那个名字了）。
    db.flush()
    _ensure_folder(db, user.id, new)
    db.commit()
    return {"ok": True, "path": new}


@router.delete("/folders")
def delete_folder(path: str, user: AuthenticatedWriter, db: DbSession) -> dict:
    """删一个分组。**里面还有东西就拒绝** —— 与 `rmdir` 一样。

    用户的会话是最贵的产物，一个误点不该连带删掉一堆；想清空就先自己挪出来。
    """
    target = _clean_folder(path)
    if not target:
        raise HTTPException(400, "分组名不能为空")
    rows = _folder_rows(db, user.id, target)
    if not rows:
        raise HTTPException(404, "没有这个分组")
    if len(rows) > 1:
        raise HTTPException(400, "这个分组里还有子分组")
    inside = db.execute(
        select(func.count())
        .select_from(Conversation)
        .where(
            Conversation.user_id == user.id,
            or_(Conversation.folder == target, Conversation.folder.like(target + "/%")),
        )
    ).scalar()
    if inside:
        raise HTTPException(400, "这个分组里还有 " + str(inside) + " 个对话")
    for folder in rows:
        db.delete(folder)
    db.commit()
    return {"ok": True}


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
    """改标题 / 置顶。两个字段都**按需**更新（`pinned` 只认真正的布尔值，
    否则 `pinned: false` 会被 `or` 当成"没给"）。"""
    conv = _own_conversation(db, user.id, cid)
    body = payload or {}

    if "title" in body:
        title = str(body.get("title") or "").strip()[:120]
        if not title:
            raise HTTPException(400, "标题不能为空")
        conv.title = title

    if isinstance(body.get("pinned"), bool):
        conv.pinned = body["pinned"]

    if "folder" in body:
        # 挪进某个分组：目标分组顺手登记一次（拖放时用户常常没先建过它）
        conv.folder = _clean_folder(body.get("folder"))
        _ensure_folder(db, user.id, conv.folder)

    # 这里**不碰** `updated_at`：改名与置顶不是"活动"，列表的"最近"排序
    # 只该被消息推动（模型上的注释解释了为什么那一列没有 `onupdate`）。
    db.commit()
    return {"ok": True, "pinned": bool(conv.pinned)}


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


@router.post("/attachments")
async def upload_attachment(
    user: AuthenticatedWriter, db: DbSession, file: UploadFile = File(...)
) -> dict:
    """上传一个附件。**先传、再在发消息时引用它**。

    为什么分两步而不是跟着消息一起 multipart 上去：发送那条路是 SSE 流，
    要在里面同时收文件、校验、落盘、再开流，会把"流"这件事和非流的事情搅在一起。
    分开之后这条路由是普通的请求-响应，失败也不用在半开的流里报错。

    抽出来的正文（PDF 走 `pdftotext`）跟着附件一起入库，供模型读；
    图片抽不出正文，但照常存下来给人看。
    """
    data = await file.read()
    result = attach.save(db, user, file.filename or "附件", file.content_type or "", data)
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


@router.get("/attachments/{aid}")
def read_attachment(aid: uuid.UUID, user: CurrentUser, db: DbSession) -> FileResponse:
    """取回附件本身（图片直接显示、文本可预览）。别人的附件按 404 处理。"""
    row = db.get(Attachment, aid)
    if row is None or row.user_id != user.id:
        raise HTTPException(404, "没有这个附件")
    path = attach.path_of(row)
    if not path.exists():
        raise HTTPException(410, "附件文件不在这台机器上（它只在本机存着）")
    return FileResponse(
        path,
        media_type=row.mime or "application/octet-stream",
        filename=row.name,
    )


EXPORT_FORMAT = "quizforge-chat/1"


def _messages_of(db: DbSession, conv: Conversation) -> list[Message]:  # noqa: ANN001
    """整个会话的消息（**全树**，按 id 升序 = 发生顺序）。

    `order_by` 不是装饰：不加它，数据库按心情给顺序（子查询计划一变就变），
    导出的文件里对话就是乱的 —— 这个洞是导出用例在全量跑时才露出来的
    （单跑时恰好是插入顺序）。
    """
    return list(
        db.scalars(
            select(Message)
            .where(Message.conversation_id == conv.id, Message.user_id == conv.user_id)
            .order_by(Message.id)
        ).all()
    )


def _markdown_of(conv: Conversation, messages: list[Message]) -> str:
    """给人读的那一份：只画**当前分支**，零件各记一笔但不抄正文。

    为什么只画当前分支：md 是拿去读与归档的，把被顶下去的分支也铺进去就成了一份
    到处是岔路的草稿。要看全树就用 json —— 那份连工具调用与每个分支都在。
    """
    lines = [f"# {conv.title or '未命名对话'}", ""]
    lines.append(
        f"> 导出时间 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        f" · 当前分支 {len(messages)} 条消息"
    )
    lines.append("")

    for message in messages:
        lines.append("## " + ("我" if message.role == "user" else "AI"))
        lines.append("")
        if (message.content or "").strip():
            lines.append(message.content.strip())
            lines.append("")

        notes = []
        for part in message.parts or []:
            kind = part.get("type")
            if kind == "tool_call":
                notes.append(
                    "工具 `" + str(part.get("name")) + "`"
                    + ("" if part.get("ok", True) else "（失败）")
                )
            elif kind == "card":
                notes.append("题卡 `" + str((part.get("payload") or {}).get("questionId")) + "`")
            elif kind == "citation":
                notes.append(
                    f"引用 `{part.get('slug')}:{part.get('startLine')}–{part.get('endLine')}`"
                )
            elif kind == "file":
                notes.append("附件 `" + str(part.get("name")) + "`")
            elif kind == "demo":
                notes.append("演示「" + str(part.get("title")) + "」（HTML 见 json 导出）")
            elif kind == "action":
                notes.append("动作凭条 " + str(part.get("kind")))
        if notes:
            lines.extend("- " + note for note in notes)
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


@router.get("/conversations/{cid}/export")
def export_conversation(
    cid: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    format: str = Query("md", description="md（给人读，当前分支）/ json（给机器读，全树）"),
) -> PlainTextResponse:
    """导出一次对话。

    两种格式的分工是刻意的：`md` 只画**当前分支**（拿去读、拿去归档）；
    `json` 带走**整棵树**（含被顶下去的分支、工具调用、令牌数）——
    所以它是"轨迹"的完整备份，而不只是"看过的那些字"。
    """
    conv = _own_conversation(db, user.id, cid)
    all_messages = _messages_of(db, conv)

    if format == "json":
        payload = {
            "format": EXPORT_FORMAT,
            "exportedAt": datetime.now(timezone.utc).isoformat(),
            "conversation": {
                "id": str(conv.id),
                "title": conv.title,
                "createdAt": conv.created_at.isoformat() if conv.created_at else "",
                "updatedAt": conv.updated_at.isoformat() if conv.updated_at else "",
            },
            "messages": [_message_out(m) for m in all_messages],
        }
        return PlainTextResponse(
            json.dumps(payload, ensure_ascii=False, indent=2),
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="chat-{str(conv.id)[:8]}.json"'},
        )

    branch = _active_path(db, conv)
    return PlainTextResponse(
        _markdown_of(conv, branch),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="chat-{str(conv.id)[:8]}.md"'},
    )


@router.get("/export")
def export_all(
    user: CurrentUser,
    db: DbSession,
    format: str = Query("json", description="只有 json：全部分支 + 全部零件"),
) -> PlainTextResponse:
    """把所有对话导出成一个文件 —— 这是"我的轨迹"的完整备份。"""
    conversations = db.scalars(
        select(Conversation).where(Conversation.user_id == user.id).order_by(Conversation.created_at)
    ).all()
    payload = {
        "format": EXPORT_FORMAT,
        "exportedAt": datetime.now(timezone.utc).isoformat(),
        "conversations": [
            {
                "id": str(conv.id),
                "title": conv.title,
                "updatedAt": conv.updated_at.isoformat() if conv.updated_at else "",
                "messages": [_message_out(m) for m in _messages_of(db, conv)],
            }
            for conv in conversations
        ],
    }
    return PlainTextResponse(
        json.dumps(payload, ensure_ascii=False, indent=2),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="quizforge-chats.json"'},
    )


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

    cont = False

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
    elif body.get("continue"):
        # 「接一轮」：**不新增用户消息**，只让 agent 看着已有的历史说话。
        #
        # 用途是"大题批完自动开口"：批改结果会由 `problem._append_grade_note`
        # 写一条 assistant 记事进对话，接着就该主 agent 就着它讲两句。
        # 不能靠"存一条用户消息"来触发 —— 库里那条 `content` 只放**用户原话**，
        # 机器生成的"继续"混进去，他会以为是自己说的。
        last = db.scalars(
            select(Message)
            .where(Message.conversation_id == conv.id)
            .order_by(Message.id.desc())
            .limit(1)
        ).first()
        if last is None:
            raise HTTPException(400, "这条对话还没有消息，没什么可接着说的")
        user_msg = last  # 只是"挂载点"：历史从它往回取，回复挂在它下面
        cont = True
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
            # 快照"这一刻的模式"：开关是随时会改的，事后再也推不出当时挂了几组。
            # 对话树就是靠它画出"对话过程里模式变过几次、各是什么"。
            mounts=json.dumps(sorted(mounts.effective(db, user.id)), ensure_ascii=False),
        )
        # 附件：先上传、后引用（见 upload_attachment 的说明）。
        # 只接受**本人**的、且还没挂到别的消息上的那些 —— 别人传的 id 猜不出来，
        # 但猜出来了也不能用。
        attach_ids = body.get("attachments") or []
        attached = []
        if attach_ids:
            for raw in attach_ids[:8]:
                try:
                    row = db.get(Attachment, uuid.UUID(str(raw)))
                except (TypeError, ValueError):
                    continue
                if row is not None and row.user_id == user.id and row.message_id is None:
                    attached.append(row)
            if attached:
                user_msg.parts = [
                    msgparts.file_part(
                        attachment_id=row.id,
                        name=row.name,
                        mime=row.mime,
                        size=row.size,
                        kind=row.kind,
                        text_chars=len(row.text),
                    )
                    for row in attached
                ]

        db.add(user_msg)
        if not conv.title.strip():
            conv.title = _title_from(content)
        conv.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(user_msg)

        for row in attached:
            row.message_id = user_msg.id
        if attached:
            db.commit()

    # 上下文 = 到这条提问为止的那一段（不含任何别的分支上的回答）
    # 模型能读图时，图片才作为图像发出去（判断见 ai_gateway.model_reads_images）
    history = _history(
        db,
        _chain(db, user_msg),
        vision=bool(conf.get("vision")),
        model=str(conf.get("model") or ""),
    )
    if cont:
        # 合成的这一句**只进请求、不进库**：它替模型点明"现在该就着什么说话"。
        # 不点的话，历史末尾是一条 assistant 记事，模型常常当自己已经答完了。
        history.append(
            {
                "role": "user",
                "content": "（以上是大题的批改结果。）就着它说几句：哪里对、哪里缺、"
                "下一步练什么。不要重复题面，也不要复述批语全文。",
            }
        )

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


@router.post("/conversations/{cid}/messages/{mid}/run")
def attach_run(
    cid: uuid.UUID, mid: int, payload: dict, user: AuthenticatedWriter, db: DbSession
) -> dict:
    """把沙箱那次运行的结果**回填到消息上**。

    ## 为什么要回填，而不是让用户点一下「把输出发给它」

    输出本来就在宿主手里（沙箱跑完 `postMessage` 回来）。让用户按一下再复制过来，
    等于把他当传话筒 —— 而这套系统里没人该做这件事。回填之后：

    * 那条消息**直接显示输出**（不必点开面板）
    * **下一轮对话时它进模型的历史**（见 `_history`）—— 模型自己就能读到自己
      那段代码跑出了什么，不必等谁转述

    按 `runId` 认领：一次运行对应一个零件；认不出来就当没这回事（不静默写坏零件）。
    """
    conv = _own_conversation(db, user.id, cid)
    message = db.get(Message, mid)
    if message is None or message.conversation_id != conv.id:
        raise HTTPException(404, "没有这条消息")

    body = payload or {}
    run_id = str(body.get("runId") or "").strip()
    if not run_id:
        raise HTTPException(400, "缺少 runId")
    text = str(body.get("text") or "")[:RUN_TEXT_LIMIT]

    # 造**新的** dict 而不是原地改：JSON 列的脏检查比较的是值，
    # 原地改完再赋值会被判成"没变"，于是什么都不写（实测就是这样静默丢的）。
    # 再加上 flag_modified 兜一道。
    parts = []
    hit = False
    for part in message.parts or []:
        if (
            isinstance(part, dict)
            and part.get("type") == "demo"
            and str(part.get("runId") or "") == run_id
        ):
            part = dict(part, run={"ok": body.get("ok") is not False, "text": text})
            hit = True
        parts.append(part)
    if not hit:
        raise HTTPException(404, "这条消息里没有这次运行")

    message.parts = parts
    flag_modified(message, "parts")
    db.commit()
    return {"ok": True, "chars": len(text)}


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
            # 只声明**已挂载**的那几组；未挂载的即使被叫到名字也不执行（纵深防御）
            mounts=mounts.effective(db, user.id),
            # 工具要知道"这是哪次对话"：push_question 靠它避开这次已经推过的题
            tool_context={"conversationId": str(conv.id)},
        ):
            kind = event["kind"]

            if kind in ("text", "docx", "pptx"):
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
                # 工具调用之前的那段话是**铺垫**，不是结论（"我先查一下…"）—— 打上 `process`，
                # 界面上它就成了可折叠的"过程"，正文里留下的才是答案。
                #
                # 为什么在这打、而不是只在界面上临时折：**存下来的零件要带着这个标记**。
                # 只在界面上折的话，刷新一下铺垫又散回正文了（用户就是这么反馈的：
                # "我这边看不到 thinking" —— 他看到的其实是散开的铺垫）。
                for earlier in reversed(parts):
                    if earlier.get("type") == "tool_call":
                        continue  # 跳过工具本身，找它前面最近的那段文字
                    if earlier.get("type") == "text" and str(earlier.get("text") or "").strip():
                        earlier["process"] = True
                        earlier["label"] = "过程"
                    break
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

                # 临时题卡（`create_question`）：模型现编的一道题，还不在任何题单里。
                # 答案随卡片交给界面（它要判分）—— 模型那一侧只留一行答案。
                draft = (event.get("payload") or {}).get("draft")
                if isinstance(draft, dict) and draft.get("id"):
                    parts.append(msgparts.draft_part(payload=draft))
                    yield _sse("draft", {"callId": event["callId"], "draft": draft})

                # 写操作也一样：工具只**提案**，界面上长出一张待确认的凭条，
                # 他点了才落到记录里（走的是收藏夹 / 错题本那两条老路）。
                # 演示沙箱：模型给的是一段自包含 HTML，界面上长成一个沙箱 iframe
                demo = (event.get("payload") or {}).get("demo")
                if isinstance(demo, dict) and demo.get("html"):
                    parts.append(
                        msgparts.demo_part(
                            title=demo.get("title") or "演示",
                            html=demo["html"],
                            run_id=demo.get("runId") or "",
                        )
                    )
                    yield _sse("demo", {"callId": event["callId"], "demo": demo})

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
