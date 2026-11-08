"""排程的**约束 IR**：健康策略与求解器之间唯一交换的东西。

为什么要有这一层（用户原话：健康不能写死、以后还要补充）：

* 健康策略（`app/wellbeing/advisories.py`）不知道 CP-SAT 是什么，它只会说
  "今晚 23:30–07:00 别排"、"今天深度最多 40 分钟"；
* 求解器也不知道"睡眠债"是什么，它只会把 `no_window` / `max_deep_minutes`
  这类规则翻成变量与约束。

两边只认下面这份**很小的词汇表**。要加一种健康策略，就多吐一条 IR；
要换求解器（CP-SAT ↔ 纯 Python），IR 一个字都不用动。

## 词汇表

硬规则（违反 = 解不可行）：

    no_window         {start:"23:30", end:"07:00", days:"all"}      这段不排
    max_minutes       {minutes:240, scope:"study"}                  每日总量上限
    max_deep_minutes  {minutes:40}                                  每日深度上限
    energy_floor      {value:0.6}                                   深度任务的最低精力
    cooldown_after    {kind:"class", minutes:30}                    某类块之后 N 分钟不排深度

软规则（进入目标函数，权重可调）：

    prefer_window          {start:"08:00", end:"12:00", bonus:8}
    alternate_domain       {maxSame:1}                              连续同域不超过 N 块
    cap_topic_minutes      {minutes:180, scope:"domain"}            单日单主题上限
    prefer_early_for_deadline {power:2}
    minimize_fragment      {weight:1}
    priority_boost         {taskId:"...", weight:50}                重排时把上次没过门禁的提前
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

HARD_RULES = frozenset(
    {"no_window", "max_minutes", "max_deep_minutes", "energy_floor", "cooldown_after"}
)
SOFT_RULES = frozenset(
    {
        "prefer_window",
        "alternate_domain",
        "cap_topic_minutes",
        "prefer_early_for_deadline",
        "minimize_fragment",
        "priority_boost",
    }
)
_ALL_RULES = HARD_RULES | SOFT_RULES

#: 建议里这些键是 IR 自己的元数据，不是规则参数
_META_KEYS = frozenset({"rule", "weight"})


@dataclass(frozen=True)
class Constraint:
    """一条约束。`weight is None` = 硬约束；否则是软约束的权重。"""

    rule: str
    source: str = ""
    weight: float | None = None
    params: dict = field(default_factory=dict)

    @property
    def hard(self) -> bool:
        return self.weight is None

    def as_dict(self) -> dict:
        return {
            "rule": self.rule,
            "source": self.source,
            "hard": self.hard,
            "weight": self.weight,
            "params": dict(self.params),
        }


def validate(constraint: Constraint) -> Constraint:
    if constraint.rule not in _ALL_RULES:
        raise ValueError(f"IR 里没有这条规则：{constraint.rule}")
    if constraint.hard and constraint.rule in SOFT_RULES:
        raise ValueError(f"{constraint.rule} 只能是软约束（要带 weight）")
    if not constraint.hard and constraint.rule in HARD_RULES:
        raise ValueError(f"{constraint.rule} 只能是硬约束（weight 必须为空）")
    return constraint


def _split(params: dict) -> tuple[dict, float | None]:
    weight = params.get("weight")
    body = {key: value for key, value in params.items() if key not in _META_KEYS}
    return body, (float(weight) if weight is not None else None)


def from_advice(advices: Iterable) -> list[Constraint]:  # noqa: ANN001
    """`wellbeing` 的 `schedule_constraint` 建议 → IR。未知规则直接抛。

    宁可炸在测试里，也不要把一条看不懂的约束悄悄丢掉 —— 丢掉的那条可能是
    "今天深度最多 0 分钟"。
    """
    from .wellbeing.base import KIND_CONSTRAINT  # noqa: PLC0415 —— 避免循环导入

    out: list[Constraint] = []
    for advice in advices:
        if getattr(advice, "kind", "") != KIND_CONSTRAINT:
            continue
        body, weight = _split(dict(advice.params or {}))
        rule = str((advice.params or {}).get("rule") or "")
        out.append(
            validate(
                Constraint(rule=rule, source=getattr(advice, "source", "") or advice.key, weight=weight, params=body)
            )
        )
    return out


def structural(options: dict, target_bed: str, target_wake: str) -> list[Constraint]:
    """与健康无关的**结构性**规则：作息边界、三餐。

    为什么这些不放在 wellbeing 里：它们不是"身体状态"，是任何一天都成立的
    常识约束。健康策略可以在此基础上再加（比如恢复期压缩总量）。
    """
    out = [
        Constraint(
            rule="no_window",
            source="structural.sleep",
            params={"start": target_bed, "end": target_wake, "days": "all"},
        ),
        # 吃饭也是硬约束：12:00–12:40 / 18:00–18:40 不排
        Constraint(
            rule="no_window",
            source="structural.meal",
            params={"start": "12:00", "end": "12:40", "days": "all"},
        ),
        Constraint(
            rule="no_window",
            source="structural.meal",
            params={"start": "18:00", "end": "18:40", "days": "all"},
        ),
    ]
    if options.get("alternateDomain", True):
        out.append(
            Constraint(
                rule="alternate_domain",
                source="structural.preference",
                weight=float(options.get("alternateWeight") or 15.0),
                params={"maxSame": 1},
            )
        )
    return out


def collect(advices: Iterable, options: dict, target_bed: str, target_wake: str) -> list[Constraint]:
    """一次把健康建议与结构性规则收齐（顺序稳定：便于测试与解释）。"""
    return structural(options, target_bed, target_wake) + from_advice(advices)


def hard_value(constraints: Iterable[Constraint], rule: str, default: float | None = None):
    """同名硬约束取**最紧**的那个（两个策略都说"最多 40 分钟"与"最多 0 分钟"）。"""
    values = [
        float(one.params.get("minutes", one.params.get("value", 0)))
        for one in constraints
        if one.rule == rule
    ]
    if not values:
        return default
    return min(values)


__all__ = [
    "HARD_RULES",
    "SOFT_RULES",
    "Constraint",
    "collect",
    "from_advice",
    "hard_value",
    "structural",
    "validate",
]
