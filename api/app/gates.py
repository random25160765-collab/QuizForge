"""质量门禁：**做完 ≠ 学会了**。

一个学习块结束、或者条目被勾完成时，按策略验一下这次到底有没有进脑子。
五种内置策略（题库测验 / 凭记忆复述 / 讲给 AI 听 / 交一个小产出物 / 不设门禁）
都实现同一对方法：

    prepare(db, item, event)           → 给用户看的那张卷子（不带答案）
    verify(db, item, event, answer)     → 判分（通过 / 不通过 + 依据）

新增一种门禁 = 新写一个类 + `register()`，路由与界面都不认具体名字
（界面只认 `kind`：`quiz` 画选项、`text` 画多行输入、`none` 直接放行）。

## 为什么没过也要落库

不通过的记录是"下一次重排该优先补偿它"的唯一依据。只记通过的话，没过的块
就静默地留在原地，第二天照旧排在最后 —— 那就等于没有门禁。

## AI 不可用时

复述 / 讲解这两种天然要人（或模型）听懂才判得准。没有 AI 就**降级为自评**，
并在 payload 里写明 `degraded: true` —— 宁可标出来它是自评，也不要假装判过。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select

from .models import KnowledgePoint, Question, QuestionPoint

#: 测验通过线（三题错一道仍然算过；错两道就是没掌握）
PASS_RATIO = 2 / 3
#: 自评的最低分（1..5）
MIN_RATING = 3


@dataclass
class GateResult:
    passed: bool
    score: float = 0.0
    detail: str = ""
    payload: dict = field(default_factory=dict)


class Gate:
    """一条门禁策略。子类只实现 `prepare` / `verify`。"""

    key = ""
    label = ""
    #: 界面画什么：quiz（选项）/ text（多行输入）/ none（直接放行）
    kind = "text"
    #: 文字类策略的最短篇幅（太短的"我记住了"没有信息量）
    min_chars = 0

    def prepare(self, db, item, event=None) -> dict:  # noqa: ANN001, ARG002
        raise NotImplementedError

    def verify(self, db, item, event, answer) -> GateResult:  # noqa: ANN001, ARG002
        raise NotImplementedError

    def _base(self, item) -> dict:  # noqa: ANN001
        return {
            "gate": self.key,
            "label": self.label,
            "kind": self.kind,
            "itemId": getattr(item, "id", None),
            "title": getattr(item, "title", ""),
            "points": list(getattr(item, "points", []) or []),
        }


# ============================================================================ 题库测验


def _quiz_pool(db, item, limit: int = 3) -> list[Question]:  # noqa: ANN001
    """这道题从哪几道里抽。**确定性**：按难度、id 排，抽到哪几道每次都一样。"""
    stmt = select(Question).where(
        Question.retired_at.is_(None),
        Question.status.in_(("published", "verified")),
        Question.type == "single",
    )
    points = [str(one) for one in (getattr(item, "points", []) or []) if str(one).strip()]
    if points:
        stmt = (
            stmt.join(QuestionPoint, QuestionPoint.question_id == Question.id)
            .join(KnowledgePoint, KnowledgePoint.id == QuestionPoint.point_id)
            .where(KnowledgePoint.key.in_(points))
            .distinct()
        )
    return list(db.scalars(stmt.order_by(Question.difficulty, Question.id).limit(limit)).all())


class QuizGate(Gate):
    key = "quiz"
    label = "题库测验"
    kind = "quiz"

    def prepare(self, db, item, event=None) -> dict:  # noqa: ANN001
        questions = _quiz_pool(db, item)
        if not questions:
            return {
                **self._base(item),
                "gate": "recall",
                "label": "题库里没有对应的题 → 改用复述",
                "kind": "text",
                "degraded": True,
                "prompt": "题库里还没有覆盖这些知识点的题，合上材料把要点复述一遍。",
                "minChars": RecallGate.min_chars,
                "passAt": PASS_RATIO,
            }
        return {
            **self._base(item),
            "degraded": False,
            "passAt": PASS_RATIO,
            "questions": [
                {
                    "id": one.id,
                    "topic": one.topic,
                    "stem": str((one.payload or {}).get("stem") or ""),
                    "options": list((one.payload or {}).get("options") or []),
                }
                for one in questions
            ],
        }

    def verify(self, db, item, event, answer) -> GateResult:  # noqa: ANN001
        questions = _quiz_pool(db, item)
        if not questions:
            return GateResult(False, 0.0, "题库里没有可判的题，先用复述门禁", {"degraded": True})
        choices = (answer or {}).get("choices") or {}
        if not isinstance(choices, dict):
            choices = {}
        correct = 0
        wrong = []
        for one in questions:
            expected = (one.payload or {}).get("answer")
            got = str(choices.get(one.id) or "").strip()
            if expected and got == str(expected):
                correct += 1
                continue
            wrong.append(
                {
                    "id": one.id,
                    "stem": str((one.payload or {}).get("stem") or ""),
                    "chose": got,
                    "answer": expected,
                    "explanation": str((one.payload or {}).get("explanation") or ""),
                }
            )
        score = correct / len(questions)
        passed = score >= PASS_RATIO
        detail = (
            f"{correct}/{len(questions)} 正确"
            if passed
            else f"只对 {correct}/{len(questions)}：错的那几道看解析再说一遍"
        )
        return GateResult(passed, score, detail, {"total": len(questions), "correct": correct, "wrong": wrong})


# ============================================================================ 复述 / 讲解 / 产出物


class _TextGate(Gate):
    kind = "text"
    prompt = ""

    def prepare(self, db, item, event=None) -> dict:  # noqa: ANN001, ARG002
        return {
            **self._base(item),
            "degraded": True,  # 没有 AI 判分时就是自评，界面上要说清楚
            "prompt": self.prompt,
            "minChars": self.min_chars,
        }

    def verify(self, db, item, event, answer) -> GateResult:  # noqa: ANN001, ARG002
        answer = answer or {}
        text = str(answer.get("text") or "").strip()
        rating = int(answer.get("selfRating") or 0)
        long_enough = len(text) >= self.min_chars
        rated = rating >= MIN_RATING
        if not long_enough:
            return GateResult(
                False,
                0.0,
                f"只写了 {len(text)} 字，至少 {self.min_chars} 字才能说明白",
                {"chars": len(text), "degraded": rating == 0},
            )
        if rating and not rated:
            return GateResult(
                False,
                rating / 5,
                "你自己也觉得没弄明白 —— 这次不算过",
                {"chars": len(text), "rating": rating, "degraded": True},
            )
        return GateResult(
            True,
            1.0 if not rating else rating / 5,
            "记下了" if not rating else "自评通过",
            {"chars": len(text), "rating": rating, "degraded": True},
        )


class RecallGate(_TextGate):
    key = "recall"
    label = "凭记忆复述"
    min_chars = 80
    prompt = "合上材料，凭记忆把这一段的要点复述出来（不许翻书；写多少算多少，但要说清楚因果）。"


class ExplainGate(_TextGate):
    key = "explain"
    label = "讲给 AI 听"
    min_chars = 120
    prompt = "讲给一个完全不懂的人听：它是干什么的、什么时候用、用错了会怎样。直到他能自己复述。"


class ArtifactGate(_TextGate):
    key = "artifact"
    label = "交一个小产出物"
    min_chars = 60
    prompt = "交一个能看见的产出物（一页流程图 / 一段能跑的代码 / 一版小结），把内容贴进来。"


class NoneGate(Gate):
    """不设门禁：有些事（"把书还了"）本来就不该考。"""

    key = "none"
    label = "不设门禁"
    kind = "none"

    def prepare(self, db, item, event=None) -> dict:  # noqa: ANN001, ARG002
        return {**self._base(item), "degraded": False, "prompt": "", "passed": True}

    def verify(self, db, item, event, answer) -> GateResult:  # noqa: ANN001, ARG002
        return GateResult(True, 1.0, "无需门禁", {})


# ============================================================================ 注册表


GATES: dict[str, Gate] = {}


def register(gate: Gate) -> Gate:
    if not gate.key:
        raise ValueError("Gate 必须有 key")
    if gate.key in GATES:
        raise ValueError(f"门禁 {gate.key} 已经注册过了")
    GATES[gate.key] = gate
    return gate


def register_builtin() -> None:
    for one in (QuizGate(), RecallGate(), ExplainGate(), ArtifactGate(), NoneGate()):
        if one.key not in GATES:
            register(one)


def resolve(db, item) -> Gate:  # noqa: ANN001
    """这条待办该走哪条门禁：逐条指定优先，否则按学科取设置里的默认映射。"""
    from . import wellbeing  # noqa: PLC0415 —— 避免循环导入

    key = str(getattr(item, "gate", "") or "").strip()
    if not key:
        defaults = wellbeing.options_of(db).get("gateDefaults") or {}
        key = str(defaults.get(getattr(item, "domain", "other") or "other") or "")
    return GATES.get(key) or GATES["none"]


def prepare(db, item, event=None) -> dict:  # noqa: ANN001
    return resolve(db, item).prepare(db, item, event)


def submit(db, item, event, answer) -> GateResult:  # noqa: ANN001
    return resolve(db, item).verify(db, item, event, answer)


def latest_failed(db, item_id: str) -> bool:  # noqa: ANN001
    """最近一次门禁是不是没过（重排据此优先补偿）。"""
    from .models import GateRun  # noqa: PLC0415 —— 避免循环导入

    row = db.scalars(
        select(GateRun)
        .where(GateRun.item_id == item_id)
        .order_by(GateRun.created_at.desc())
        .limit(1)
    ).first()
    return row is not None and not row.passed


def priority_constraints(db) -> list:  # noqa: ANN001
    """门禁没过 → 一条 `priority_boost` 软约束（**下一次重排先补它**）。

    这是"门禁不是打分，是调度输入"的落点：不过的题不是被记一笔就完了，
    它会在下一周的排程里往前站。
    """
    from .models import GateRun, PlanItem  # noqa: PLC0415 —— 避免循环导入
    from .schedule_ir import Constraint  # noqa: PLC0415

    rows = db.scalars(
        select(GateRun)
        .where(GateRun.passed.is_(False), GateRun.item_id.is_not(None))
        .order_by(GateRun.created_at.desc())
    ).all()
    out = []
    seen: set[str] = set()
    for row in rows:
        if not row.item_id or row.item_id in seen:
            continue
        seen.add(row.item_id)
        item = db.get(PlanItem, row.item_id)
        if item is None or item.done:
            continue
        out.append(
            Constraint(
                rule="priority_boost",
                source="gates.failed",
                weight=50.0,
                params={"taskId": item.id},
            )
        )
    return out


register_builtin()


__all__ = [
    "GATES",
    "ArtifactGate",
    "ExplainGate",
    "Gate",
    "GateResult",
    "NoneGate",
    "PASS_RATIO",
    "QuizGate",
    "RecallGate",
    "latest_failed",
    "prepare",
    "priority_constraints",
    "register",
    "resolve",
    "submit",
]
