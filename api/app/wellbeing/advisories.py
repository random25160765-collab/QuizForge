"""内置建议策略：把"今天该怎么对自己"写成结构化建议。

三条去向（`kind`）：

* `schedule_constraint` —— 给排程器的约束 IR（`params` 就是 IR 的原始参数）；
* `chat_intervention`  —— 给对话的一句话（到点该睡了、今天别再学了）；
* `ui_note`            —— 给界面的说明（今晚目标几点睡）。

**对话侧只读 `chat_intervention`**，不判断"现在是不是 bedtime"—— 加一种
"病了就早点休息"的建议，不用碰对话代码。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from datetime import time as time_type

from .base import (
    KIND_CHAT,
    KIND_CONSTRAINT,
    KIND_UI,
    Advisory,
    Advice,
    Context,
    EnergyCurve,
    Signal,
    register_advisory,
)


def _hm(value: time_type) -> str:
    return f"{value.hour:02d}:{value.minute:02d}"


def _at(day: datetime, clock: time_type) -> datetime:
    return day.replace(hour=clock.hour, minute=clock.minute, second=0, microsecond=0)


class BedtimeCurfew(Advisory):
    """到点劝睡：一条排程约束（睡眠时段不排） + 到点后的对话劝告 + 一条界面提示。"""

    key = "bedtime_curfew"
    label = "睡眠时段"

    def advise(self, ctx: Context, signals: dict[str, Signal], curve: EnergyCurve) -> list[Advice]:
        if not ctx.options.get("curfew", True):
            return []

        bed, wake = ctx.target_bed, ctx.target_wake
        grace = int(ctx.options.get("graceMin") or 0)
        out = [
            Advice(
                key=f"{self.key}.window",
                kind=KIND_CONSTRAINT,
                severity="hard",
                text=f"睡眠时段 {_hm(bed)}–{_hm(wake)} 不排任何事",
                params={"rule": "no_window", "start": _hm(bed), "end": _hm(wake), "days": "all"},
                source=self.key,
            ),
            Advice(
                key=f"{self.key}.note",
                kind=KIND_UI,
                severity="info",
                text=f"今晚 {_hm(bed)} 前睡，明早 {_hm(wake)} 起",
                params={"bed": _hm(bed), "wake": _hm(wake)},
                source=self.key,
            ),
        ]

        bedtime = _at(ctx.now, bed)
        if bedtime <= ctx.now - timedelta(hours=6):
            # 已经过了午夜：今晚的目标是"现在立刻"，而不是今天的 23:30
            bedtime = bedtime + timedelta(days=1)
        late = (ctx.now - bedtime).total_seconds() / 60.0
        if late >= 0:
            minutes = int(late)
            out.append(
                Advice(
                    key=f"{self.key}.now",
                    kind=KIND_CHAT,
                    severity="hard" if minutes >= grace else "warn",
                    text=(
                        f"已经 {_hm(ctx.now.time())}，比目标睡觉时间晚了 {minutes} 分钟。"
                        "现在只回一句劝对方去睡，不要再展开任何话题。"
                    ),
                    params={"lateMin": minutes, "bed": _hm(bed), "graceMin": grace},
                    source=self.key,
                )
            )
        elif (bedtime - ctx.now).total_seconds() <= 30 * 60:
            out.append(
                Advice(
                    key=f"{self.key}.soon",
                    kind=KIND_UI,
                    severity="warn",
                    text=f"还有 {int((bedtime - ctx.now).total_seconds() // 60)} 分钟到睡觉时间",
                    params={"bed": _hm(bed)},
                    source=self.key,
                )
            )
        return out


class DeepBudget(Advisory):
    """把精力曲线算出来的深度预算变成一条排程约束（求解器只认这个数）。"""

    key = "deep_budget"
    label = "深度预算"

    def advise(self, ctx: Context, signals: dict[str, Signal], curve: EnergyCurve) -> list[Advice]:
        floor = float(ctx.params.get("deepEnergyFloor") or 0.6)
        return [
            Advice(
                key=f"{self.key}.cap",
                kind=KIND_CONSTRAINT,
                severity="hard",
                text=f"今天深度用脑最多 {curve.deep_budget_min} 分钟",
                params={"rule": "max_deep_minutes", "minutes": int(curve.deep_budget_min)},
                source=self.key,
            ),
            Advice(
                key=f"{self.key}.floor",
                kind=KIND_CONSTRAINT,
                severity="hard",
                text=f"深度任务只在精力 ≥ {floor:g} 的时段",
                params={"rule": "energy_floor", "value": floor},
                source=self.key,
            ),
        ]


class SleepDebtGuard(Advisory):
    """欠觉太多时压总量：不是"今天别学"，而是"今天只学一小段"。"""

    key = "sleep_debt_guard"
    label = "欠觉保底"

    #: 欠到这个量（小时）才出手 —— 少欠一点不该被当成病人
    threshold_hours = 3.0

    def advise(self, ctx: Context, signals: dict[str, Signal], curve: EnergyCurve) -> list[Advice]:
        debt = float((signals.get("sleep_debt").raw or {}).get("debtHours") or 0)
        if debt < self.threshold_hours:
            return []
        cap = int(clamp_minutes(300 - (debt - self.threshold_hours) * 40))
        return [
            Advice(
                key=f"{self.key}.cap",
                kind=KIND_CONSTRAINT,
                severity="warn",
                text=f"近七天欠 {debt:g} 小时，今天学习总量压到 {cap} 分钟",
                params={"rule": "max_minutes", "minutes": cap, "scope": "study"},
                source=self.key,
            ),
            Advice(
                key=f"{self.key}.chat",
                kind=KIND_CHAT,
                severity="warn",
                text="对方最近睡眠不足。安排任务时按「只做一小段」来劝，不要一次给一大堆。",
                params={"debtHours": debt},
                source=self.key,
            ),
        ]


class RecoveryDay(Advisory):
    """恢复期档位：深度任务一律不排（浅的可以，别逼自己）。"""

    key = "recovery_day"
    label = "恢复期"

    def advise(self, ctx: Context, signals: dict[str, Signal], curve: EnergyCurve) -> list[Advice]:
        if ctx.profile_key != "recovery":
            return []
        return [
            Advice(
                key=f"{self.key}.no-deep",
                kind=KIND_CONSTRAINT,
                severity="warn",
                text="恢复期不排深度任务",
                params={"rule": "max_deep_minutes", "minutes": 0},
                source=self.key,
            ),
            Advice(
                key=f"{self.key}.note",
                kind=KIND_UI,
                severity="warn",
                text="恢复期：能少排就少排，别硬撑",
                params={},
                source=self.key,
            ),
        ]


def clamp_minutes(value: float) -> int:
    return max(60, min(300, int(round(value / 10) * 10)))


def register_builtin_advisories() -> None:
    for advisory in (BedtimeCurfew(), DeepBudget(), SleepDebtGuard(), RecoveryDay()):
        register_advisory(advisory)


__all__ = [
    "BedtimeCurfew",
    "DeepBudget",
    "RecoveryDay",
    "SleepDebtGuard",
    "register_builtin_advisories",
]
