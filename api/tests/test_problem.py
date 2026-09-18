"""大题的契约：题面不带答案、子代理的工具集是收窄的、批改会写进记录。

这三条是这件事能成立的前提 —— 少任何一条，大题就会变成"看起来像其它题、
其实另立了一套状态"的东西。
"""

from __future__ import annotations

import json
import uuid

from app import tools
from app.models import Question, Record, User
from app.routers import problem as problem_router

PASSWORD = "password-1234"


def _register(client) -> None:  # noqa: ANN001
    """本机用户就绪（各家测试各写一份，不互相 import）。见 `app/deps.py`。"""
    from conftest import local_user_id

    local_user_id()


def _me_id(client) -> str:  # noqa: ANN001
    """本机用户的 id。"""
    from conftest import local_user_id

    return local_user_id()


def _seed_problem(db, *, index: int = 1) -> Question:  # noqa: ANN001
    """造一道两问的大题（带参考答案，用来验证它**不会**漏出去）。"""
    question = Question(
        id="tt-prob-" + uuid.uuid4().hex[:8],
        type="problem",
        layer="应用",
        wing="综合",
        difficulty=3,
        topic="测试用大题",
        status="published",
        content_hash=uuid.uuid4().hex + uuid.uuid4().hex,  # NOT NULL，造个不重样的
        payload={
            "title": "测试大题",
            "stem": "题干：一个 3 op 的流水线在 CI 上偶发 hang。",
            # 小问的真实键是 `parts`（不是 `questions`）
            "parts": [
                {
                    "index": 1,
                    "title": "第 1 问｜先怎么切",
                    "stem": "第一问的题面。",
                    "hint": "从最小化开始想。",
                },
                {
                    "index": 2,
                    "title": "第 2 问｜再看 cache",
                    "stem": "第二问的题面。",
                },
            ],
            "reference": "参考解：先固定随机种子，再逐个 op 二分。",
            "rubric": [{"point": "说出固定种子", "weight": 1}],
        },
    )
    db.add(question)
    db.flush()
    return question


def test_problem_payload_never_carries_the_reference(client, db_session) -> None:  # noqa: ANN001
    """发出去的题面**不含参考答案与评分要点**。

    它既要进模型上下文，又会被渲染在子窗口里 —— 答案摆在眼前就等于没题可做。
    子代理批改时自己会用工具去取参考解，那是它主动要看的。
    """
    _register(client)
    question = _seed_problem(db_session)
    out = problem_router._problem_out(question, db_session)

    text = json.dumps(out, ensure_ascii=False)
    assert "参考解" not in text and "固定种子" not in text
    assert question.id in text and "第一问的题面" in text
    assert [item["index"] for item in out["questions"]] == [1, 2]
    assert out["questions"][0]["hint"] == "从最小化开始想。"


def test_the_subagent_toolbox_is_narrowed(client, db_session) -> None:  # noqa: ANN001
    """子代理只拿得到那四个工具 —— 收窄是"子"的一半。

    尤其是**没有** run_python：它跑在沙箱里、输出进不了子代理的上下文，
    给了它只会让它凭想象说"我算了一下"（那条坑刚填过）。
    """
    _register(client)
    user = db_session.get(User, uuid.UUID(_me_id(client)))
    box = problem_router._Toolbox(problem_router.GRADE_TOOLS)

    names = [spec["function"]["name"] for spec in box.specs()]
    assert names == list(problem_router.GRADE_TOOLS)
    assert "run_python" not in names and "render_demo" not in names

    # 名单外的工具回一条结果（不是抛）：模型看得见"不在范围内"
    ok, payload = box.call(db_session, user, "push_question", {})
    assert ok is False and "不在大题批改的范围内" in payload["error"]


def test_grading_writes_into_the_same_records_as_other_questions(client, db_session) -> None:  # noqa: ANN001
    """批改落进 `records` —— 掌握度与间隔重复因此**不必为大题另立一套**。"""
    _register(client)
    user = db_session.get(User, uuid.UUID(_me_id(client)))
    question = _seed_problem(db_session)

    ok, payload = tools.call(
        db_session,
        user,
        "grade_problem",
        {
            "questionId": question.id,
            "verdicts": [
                {"index": 1, "verdict": "correct", "score": 1, "comment": "切法对"},
                {"index": 2, "verdict": "wrong", "score": 0, "comment": "漏了 cache 那一支"},
            ],
        },
    )
    assert ok, payload
    assert payload["recorded"] is True
    assert payload["status"] == "partial"  # 一对一错 → 部分对
    assert payload["score"] == 0.5

    record = db_session.scalar(
        select_record(db_session, user.id, question.id)
    )
    assert record.attempts == 1
    assert record.partial == 1 and record.correct == 0 and record.wrong == 0
    assert record.last_status == "partial" and record.last_score == 0.5
    # 逐问判定留在 patch 里（讲评之后还能回看当时判了什么）
    history = (record.patch or {}).get("problem") or []
    assert history and len(history[0]["verdicts"]) == 2
    assert history[0]["verdicts"][1]["comment"].startswith("漏了 cache")


def select_record(db, user_id, question_id):  # noqa: ANN001
    from sqlalchemy import select

    return select(Record).where(Record.user_id == user_id, Record.question_id == question_id)
