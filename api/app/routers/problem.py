"""大题 —— 批改的那条独立路径。

**注意：对话页已经不再走这里了。** 题库里的大题现在与临时大题**同一条路**：
推成一张题卡（`push_question` / `create_question`，`CARD_TYPES` 里已经含 problem）、
在对话页按"每问一个输入区"作答、答完点「发送给 AI」交给**本页那个模型**批改
（用户："目前题库里的大题是单独界面单独子代理 —— 这个改成和临时大题一样的配置"）。
输入区那颗「大题」按钮也已删掉，所以子窗口那条路不再有入口。

这个模块保留着：端点还在（`/api/problem/*`），刷题页与历史数据仍可能用到；
下面这套"专职子代理批改"的设计也仍然有效 —— 只是不再是对话页的那条路。

---

原设计（仍然成立的部分）：

题卡（`push_question`）承载的是"可点选、可填空"的题：判分是确定的，前端自己就能算。
大题不是：它有多问、要写推导或代码，答案是**一段论证** —— 判分只能由模型来，
而且要分问反馈、能追问、能顺着他的错处往下问。所以它曾是另一套：

* **出题**：从题库挑一道 `problem`（含小问），发出去的**不带参考答案**
* **作答**：分问写（推导、代码、算过的数都可以）
* **批改**：起一个**子代理** —— 自己的提示词、自己的上下文（不带聊天记录）、
  以及一个**收窄过的工具集**。它不能推题、不能画演示、不能跑 Python
  （跑了它也看不到输出），只做三件事：查参考解、查材料原文、把结果写进记录

最后一条是关键：批改结果落进 `records`，于是**掌握度与间隔重复与其它题同一条路**，
大题不必另立一套。这也是"子代理"在这里的价值 —— 它只负责判，不负责攒状态。
"""

from __future__ import annotations

import json
import re
import time

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from .. import agent_loop, ai_gateway as gateway, tools
from ..deps import DbSession
from ..models import KnowledgePoint, Message, Question, QuestionPoint, Record

router = APIRouter(prefix="/api/problem", tags=["problem"])

# 子代理能用的工具（收窄是"子"的一半）。刻意**没有** run_python：
# 输出落在面板上、不进它的上下文，它会凭想象说自己算出来了 —— 那条坑刚填过。
GRADE_TOOLS = (
    "get_existing_questions",
    "search_material",
    "read_material",
    "grade_problem",
)

SUBAGENT_PROMPT = (
    "你是一位**阅卷与教练**，正在批一道大题。服务对象是一个啃 AI 加速器"
    "（Tenstorrent / tt-metal）的学习者。用中文。\n"
    "你的职责是**判**与**带**，不是替他做题：\n"
    "1. 先取参考解与评分要点（`get_existing_questions(includeAnswer=true)`），"
    "必要时用 `search_material` / `read_material` 把材料原文调出来对照；"
    "凡是指出他错在哪，都要给得出依据（材料名 + 行号，别凭记忆编）。\n"
    "2. **逐问批**，每问先说他对的部分（哪怕只是一半），再指出缺口 —— "
    "缺口要具体到「少了哪一步／哪个量」，不要说「不够深入」。\n"
    "3. 批完**必须**调 `grade_problem` 把逐问判定写进记录（不写就不算数）。\n"
    "4. 不要把参考答案整段抄给他（他要的是会做，不是抄一遍）；他明确要才给。\n"
    "5. 最后可以顺着他最弱的那一问追问半句，让他接着想 —— 但别一口气问三个问题。\n"
    "语气：像实验室里带你的师兄，直接、不客套、不空泛鼓励。\n"
    "**所有输出一律用中文** —— 包括调工具前后那半句话。"
    "（实测：这段里不加这一句，它会在工具调用前用英文自言自语。任何情况下都不要输出英文句子。）\n"
)


class _Toolbox:
    """把整套工具**收窄**成子集。

    `agent_loop` 只要求 `specs() / call() / output_text()` 三件东西，
    所以不需要改循环本身 —— 子代理之所以是"子"，一半就在这个收窄上。
    """

    def __init__(self, names) -> None:  # noqa: ANN001
        self._names = tuple(names)

    def specs(self) -> list[dict]:
        return [
            spec for spec in tools.specs() if spec["function"]["name"] in self._names
        ]

    def call(self, db, name, args, ctx=None):  # noqa: ANN001
        # 名单外的直接回一条结果（不是抛）：模型看得见"这个不在范围内"，
        # 比它以为工具坏了要好
        if name not in self._names:
            return False, {"error": "这个工具不在大题批改的范围内：" + str(name)}
        return tools.call(db, name, args, ctx)

    def output_text(self, payload: dict) -> str:
        return tools.output_text(payload)


def _sse(name: str, payload: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _problem_out(question: Question, db) -> dict:  # noqa: ANN001
    """题面（**不含**参考答案与评分要点）。

    和题卡同一条规矩：它要进模型上下文、也会被渲染出来 —— 答案摆在眼前就等于
    没题可做了。判分时子代理自己会用工具去取参考解，那是它主动要看的。
    （大题的 payload 里确实有 `reference` / `rubric` / `answer`，全都留着不往外发。）

    小问在 `payload["parts"]` 里（不是 `questions` —— 那是这一层**对外的**字段名，
    两个名字不一样，实测按直觉猜错过一次）。
    """
    payload = question.payload or {}
    stem = str(payload.get("stem") or "").strip()

    def title_from(text: str) -> str:
        """没有 title 就取题干首行 —— topic 是学科名（tt-arch），当标题没意义。"""
        first = text.splitlines()[0] if text else ""
        cleaned = re.sub(r"[*#`>]", "", first).strip()
        return cleaned[:48] or "大题"

    questions = []
    for index, sub in enumerate(payload.get("parts") or [], start=1):
        if not isinstance(sub, dict):
            continue
        questions.append(
            {
                "index": int(sub.get("index") or index),
                "title": str(sub.get("title") or "").strip(),
                "stem": str(sub.get("stem") or "").strip(),
                "hint": str(sub.get("hint") or "").strip(),
            }
        )

    return {
        "id": question.id,
        "title": str(payload.get("title") or "").strip() or title_from(stem),
        "stem": stem,
        "layer": question.layer,
        "wing": question.wing,
        "difficulty": question.difficulty,
        "topic": question.topic,
        "count": len(questions),
        "questions": questions,
    }


@router.get("/next")
def next_problem(
    db: DbSession,
    pointKey: str = Query("", description="限定知识点（可省略）"),
    maxDifficulty: int = Query(0, description="难度上限 1–5，0 表示不限"),
) -> dict:
    """挑一道大题（不含参考答案）。

    选题规则与 `/api/picks`、`push_question` 同一套，**刻意写得笨**：
    筛选 → 排掉已经答对的 → 优先没做过的 → 同分按题号。可解释、可复现，
    否则"为什么给我这道"永远说不清。
    """
    stmt = (
        select(Question)
        .where(
            Question.retired_at.is_(None),
            Question.type == "problem",
            Question.status == "published",
        )
    )
    if maxDifficulty:
        stmt = stmt.where(Question.difficulty <= maxDifficulty)
    point_key = str(pointKey or "").strip()
    if point_key:
        stmt = (
            stmt.join(QuestionPoint, QuestionPoint.question_id == Question.id)
            .join(KnowledgePoint, KnowledgePoint.id == QuestionPoint.point_id)
            .where(KnowledgePoint.key == point_key)
        )

    rows = db.scalars(stmt.order_by(Question.id).limit(400)).all()
    if not rows:
        return {"problem": None, "note": "这个范围里没有大题。"}

    records = {
        record.question_id: record
        for record in db.scalars(select(Record)).all()
    }
    fresh, missed = [], []
    for question in rows:
        record = records.get(question.id)
        if record is not None and record.attempts and record.last_status == "correct":
            continue  # 答对了就是会了
        (fresh if record is None or not record.attempts else missed).append(question)
    pool = fresh or missed
    if not pool:
        return {
            "problem": None,
            "note": "这个范围内的大题他都答对了 —— 把难度上限放宽，或者换个知识点。",
        }

    chosen = pool[0]
    record = records.get(chosen.id)
    return {
        "problem": _problem_out(chosen, db),
        "attempts": (record.attempts if record else 0) or 0,
        "note": "题面里不含参考答案（子代理批改时自己会去取）。让他分问作答，答完提交。",
    }


GRADE_NOTE_MAX = 1600


def _append_grade_note(db, conversation_id: str, question, record, feedback: str) -> None:  # noqa: ANN001
    """把批改结果写一条消息进对话 —— 这是"串行"的那根线。

    子代理的结论原先只活在界面那个小窗口里，主 agent 对它一无所知：
    它下一轮照样问"要不要来道大题"，或者对刚发生的批改毫无反应。
    写进对话之后，主 agent 读历史就看得见"他做过这道大题、批成什么样"。

    角色是 assistant、零件是 `summary`：**它不是用户说的话**（库里那条规矩：
    `content` 只存用户原话），而是一条"刚才发生了什么"的记事。
    """
    import uuid as _uuid

    text = (feedback or "").strip()
    if not text:
        return
    try:
        cid = _uuid.UUID(conversation_id)
    except (TypeError, ValueError):
        return

    last = db.scalars(
        select(Message)
        .where(Message.conversation_id == cid)
        .order_by(Message.id.desc())
        .limit(1)
    ).first()

    score = getattr(record, "last_score", None)
    status = {"correct": "全对", "partial": "部分对", "wrong": "不对"}.get(
        getattr(record, "last_status", "") or "", "已记录"
    )
    head = (
        f"【大题批改】{question.id} · {status}"
        + (f" · {score}" if score not in (None, "") else "")
        + f" · 第 {getattr(record, 'attempts', 0) or 1} 次作答"
    )
    body = text[:GRADE_NOTE_MAX]

    db.add(
        Message(
            conversation_id=cid,
            parent_id=last.id if last else None,
            role="assistant",
            content=head + "\n\n" + body,
            status="done",
            error="",
            finish_reason="stop",
            model="subagent:problem-grade",
            prompt_tokens=0,
            completion_tokens=0,
            latency_ms=0,
            parts=[{"type": "summary", "text": head + "\n\n" + body}],
        )
    )
    db.commit()


@router.post("/solve")
def solve(
    payload: dict, db: DbSession
) -> StreamingResponse:
    """批一作答：起子代理，逐问批，并把结果写进记录。

    这是这一层唯一会改状态的入口（经由子代理调 `grade_problem`）。
    上下文**不带聊天记录**：它是子代理，只看得见这道题与这次的作答 ——
    这样才能保证它的判断只基于题本身，而不是被前面聊过什么带偏。
    """
    body = payload or {}
    problem_id = str(body.get("problemId") or "").strip()
    question = db.get(Question, problem_id) if problem_id else None
    if question is None or question.retired_at is not None or question.type != "problem":
        raise HTTPException(404, "题库里没有这道大题：" + (problem_id or "(空)"))

    answers = []
    for item in (body.get("answers") or [])[:12]:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        answers.append({"index": int(item.get("index") or len(answers) + 1), "text": text[:6000]})
    if not answers:
        raise HTTPException(400, "至少答一问（每问要有一段文字）。")

    conf = gateway.resolve_config(db)
    gateway.enforce_quota(db)

    problem = _problem_out(question, db)
    lines = [
        "【大题】" + str(problem["title"]),
        "题号：" + str(problem["id"]),
        "",
        str(problem["stem"]),
    ]
    for sub in problem["questions"]:
        lines.append("")
        lines.append(f"第 {sub['index']} 问｜{sub['title']}")
        lines.append(str(sub["stem"]))
    lines.append("")
    lines.append("=== 他的作答 ===")
    for answer in answers:
        lines.append("")
        lines.append(f"第 {answer['index']} 问：")
        lines.append(str(answer["text"]))
    lines.append("")
    lines.append("请逐问批（第 1 步先取参考解与评分要点），批完调 grade_problem 写记录。")

    history = [{"role": "user", "content": "\n".join(lines)}]
    box = _Toolbox(GRADE_TOOLS)
    started = time.perf_counter()

    def stream():  # noqa: ANN202
        usage = {}
        failed = False
        graded: list[str] = []  # 批语全文：批完要写回对话（见 `_append_grade_note`）
        try:
            # 批改**不开思考**（`thinking` 不传 = 默认关）：它按 rubric 逐条对照，
            # 思维链不展示给用户、只会更慢更贵。要开就在这一处传 `thinking=True`。
            for event in agent_loop.run(
                db,
                conf,
                system=SUBAGENT_PROMPT,
                history=history,
                tools=box,
                tool_context={"problemId": str(question.id), "kind": "problem-grade"},
                max_turns=6,
            ):
                kind = event.get("kind")
                if kind in ("text", "docx", "pptx"):
                    graded.append(str(event.get("text") or ""))
                    yield _sse("delta", {"text": event["text"]})
                elif kind == "think":
                    yield _sse("think", {"text": event["text"]})
                elif kind == "note":
                    yield _sse("note", {"text": event["text"]})
                elif kind == "tool_start":
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
                    yield _sse(
                        "tool",
                        {
                            "phase": "result",
                            "callId": event["callId"],
                            "name": event["name"],
                            "ok": event["ok"],
                            "ms": event.get("ms", 0),
                            "output": event.get("output", ""),
                        },
                    )
                elif kind == "finish":
                    usage = event.get("usage") or {}
                elif kind == "error":
                    failed = True
                    yield _sse("error", {"text": event.get("text", "批改失败")})
        finally:
            gateway.record_usage(
                db,
                ok=not failed,
                latency_ms=int((time.perf_counter() - started) * 1000),
                prompt_tokens=int(usage.get("prompt_tokens") or usage.get("promptTokens") or 0),
                completion_tokens=int(
                    usage.get("completion_tokens") or usage.get("completionTokens") or 0
                ),
                detail="problem-grade",
            )
            # 批完之后把这条题的最新记录回给界面：它才是"这次算不算数"的答案
            record = db.scalar(
                select(Record).where(Record.question_id == question.id)
            )
            # 先写回对话再报 `done`：界面收到 done 可能就去重读这条对话了，
            # 那条记事先落库，它才看得见（见 `_append_grade_note`）。
            if not failed:
                _append_grade_note(
                    db,
                    str(body.get("conversationId") or ""),
                    question,
                    record,
                    "".join(graded),
                )
            yield _sse(
                "record",
                {
                    "attempts": (record.attempts if record else 0) or 0,
                    "lastStatus": (record.last_status if record else "") or "",
                    "lastScore": (record.last_score if record else None),
                },
            )
            yield _sse("done", {"problemId": str(question.id)})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


__all__ = ["router", "SUBAGENT_PROMPT", "GRADE_TOOLS"]
