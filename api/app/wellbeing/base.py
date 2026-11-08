"""健康模块的扩展点：三个协议 + 一张注册表 + 一组数据结构。

**这里是框架，不是公式。** 用户原话：

> 健康管理也要做抽象策略，绝对不能写死，因为不同时期我的身体机能差异很大，
> 而且我后续还会对这些东西进行补充。

所以核心只认四个概念：

* `Metric`   —— 从原始记录算一个信号（睡眠债、昨晚睡了多久、今天已经排了多少…）；
* `Profile`  —— 把信号组合成 48 个半小时槽的精力曲线 + 深度用脑预算；
* `Advisory` —— 把"今天该怎么对自己"写成**结构化**建议（排程约束 / 对话劝告 / 界面提示）；
* `Regime`   —— 数据表里的时间线：某段日期按哪个 Profile 算（在 `models.py`）。

新增一种看法 = 新写一个类 + `register_*()`，路由、求解器、对话都不认具体名字。
这条不能只写在注释里：`api/tests/test_wellbeing.py` 会在用例里现场注册一个假的
Metric 与 Advisory，断言它们必须出现在 `/api/wellbeing/today` 与约束 IR 里。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any

#: 一天切成 48 个半小时槽（00:00–00:30 是第 0 槽）。与日程页的 30 分钟对齐一致。
SLOTS = 48
SLOT_MINUTES = 30

#: 建议的三种去向。**不要**在这里加"睡眠"之类具体事由 —— 事由属于 Advisory。
KIND_CONSTRAINT = "schedule_constraint"
KIND_CHAT = "chat_intervention"
KIND_UI = "ui_note"


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def slot_of(moment: datetime) -> int:
    """某个时刻落在第几个半小时槽（0..47）。"""
    return min(SLOTS - 1, max(0, (moment.hour * 60 + moment.minute) // SLOT_MINUTES))


# ============================================================================ 数据


@dataclass(frozen=True)
class Night:
    """一晚的记录（两端可能缺一端）。时刻都已转成本地时区。"""

    bed_at: datetime | None
    wake_at: datetime | None
    quality: int = 0
    source: str = "manual"

    @property
    def minutes(self) -> int:
        """睡了多久；缺一端或数据不讲理时是 0（= 不知道，别拿它当"没睡"）。"""
        if not self.bed_at or not self.wake_at:
            return 0
        span = int((self.wake_at - self.bed_at).total_seconds() // 60)
        return span if 0 < span <= 24 * 60 else 0

    @property
    def known(self) -> bool:
        return self.minutes > 0

    @property
    def bed_clock(self) -> time | None:
        return self.bed_at.time() if self.bed_at else None


@dataclass(frozen=True)
class Signal:
    """一个指标算出来的东西。`value` 统一成"1 = 这个信号满格"。"""

    key: str
    label: str
    value: float
    #: 可选：48 槽的形状（昼夜节律这种天生是曲线而不是一个数）
    slot: tuple[float, ...] = ()
    raw: dict = field(default_factory=dict)
    note: str = ""


@dataclass
class EnergyCurve:
    """一天的精力曲线 + 当天深度用脑预算。"""

    slots: list[float]
    deep_budget_min: int
    headline: str
    reasons: list[str] = field(default_factory=list)

    def daytime(self) -> list[float]:
        """只看 07:00–23:00 这一段（界面上的早/午/晚三段从这里来）。"""
        return self.slots[14:46]


@dataclass(frozen=True)
class Advice:
    """一条结构化建议。

    `params` 是**约束 IR** 的原始参数（见 `app/schedule_ir.py`）：排程器只认 IR，
    不认"因为昨晚没睡好"这种理由。对话的催睡也只读 `kind == chat_intervention`。
    """

    key: str
    kind: str
    severity: str
    text: str
    params: dict = field(default_factory=dict)
    source: str = ""

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "kind": self.kind,
            "severity": self.severity,
            "text": self.text,
            "params": dict(self.params),
            "source": self.source or self.key,
        }


@dataclass
class Context:
    """算出今天所有东西所需的输入。**纯数据**，不碰数据库。"""

    now: datetime
    today: date
    nights: tuple[Night, ...] = ()
    #: 目标睡眠（分钟）与目标入睡 / 起床时刻（本地时间）
    target_min: int = 450
    target_bed: time = time(23, 30)
    target_wake: time = time(7, 0)
    #: 深度时段偏好：morning / evening / both
    deep_pref: str = "morning"
    #: 今天已经排在学习上的分钟（含计划块），与课表时长分开
    planned_min: int = 0
    class_min: int = 0
    #: 生效的档位（没有就是 None，用默认 profile）
    profile_key: str = "standard"
    regime: dict | None = None
    #: 合并后的 profile 参数（默认值 + 档位覆盖）
    params: dict = field(default_factory=dict)
    #: 设置里 `health` 那一块（合并过默认值）
    options: dict = field(default_factory=dict)


# ============================================================================ 协议


class Metric:
    """一个信号。子类只实现 `compute`，`key` 全局唯一。"""

    key = ""
    label = ""
    #: 需要回看几晚（只是提示，`Context.nights` 已按最长需求取够）
    horizon_days = 7

    def compute(self, ctx: Context) -> Signal:  # pragma: no cover - 协议
        raise NotImplementedError


class Profile:
    """一套精力算法。子类实现 `curve`，可以覆盖 `defaults`。"""

    key = ""
    label = ""
    #: 默认参数；档位（`health_regimes.params`）在这个基础上覆盖
    defaults: dict[str, Any] = {}

    def curve(self, ctx: Context, signals: dict[str, Signal]) -> EnergyCurve:  # pragma: no cover
        raise NotImplementedError


class Advisory:
    """一条建议策略。返回 0..N 条 `Advice`（可以什么都不说）。"""

    key = ""
    label = ""

    def advise(
        self, ctx: Context, signals: dict[str, Signal], curve: EnergyCurve
    ) -> list[Advice]:  # pragma: no cover - 协议
        raise NotImplementedError


# ============================================================================ 注册表

METRICS: dict[str, Metric] = {}
PROFILES: dict[str, Profile] = {}
ADVISORIES: dict[str, Advisory] = {}


def register_metric(metric: Metric) -> Metric:
    """注册一个指标。重名直接炸：注册表是要长期长的，悄悄覆盖最难查。"""
    if not metric.key:
        raise ValueError("Metric 必须有 key")
    if metric.key in METRICS:
        raise ValueError(f"指标 {metric.key} 已经注册过了")
    METRICS[metric.key] = metric
    return metric


def register_profile(profile: Profile) -> Profile:
    if not profile.key:
        raise ValueError("Profile 必须有 key")
    if profile.key in PROFILES:
        raise ValueError(f"精力档位 {profile.key} 已经注册过了")
    PROFILES[profile.key] = profile
    return profile


def register_advisory(advisory: Advisory) -> Advisory:
    if not advisory.key:
        raise ValueError("Advisory 必须有 key")
    if advisory.key in ADVISORIES:
        raise ValueError(f"建议策略 {advisory.key} 已经注册过了")
    ADVISORIES[advisory.key] = advisory
    return advisory


__all__ = [
    "ADVISORIES",
    "KIND_CHAT",
    "KIND_CONSTRAINT",
    "KIND_UI",
    "METRICS",
    "PROFILES",
    "SLOTS",
    "SLOT_MINUTES",
    "Advisory",
    "Advice",
    "Context",
    "EnergyCurve",
    "Metric",
    "Night",
    "Profile",
    "Signal",
    "clamp",
    "register_advisory",
    "register_metric",
    "register_profile",
    "slot_of",
]
