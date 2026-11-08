"""内置指标：把原始记录变成 0..1 的信号。

每个指标只说**一件事**，而且只在 `raw` 里放能复述的数值（"欠 3.5 小时"），
不在指标里下结论（"今天该少排点"是 Profile 与 Advisory 的事）。
新增指标请照这里的形状写，并在 `wellbeing/__init__.py` 里注册。
"""

from __future__ import annotations

from .base import SLOTS, Context, Metric, Signal, clamp, register_metric


class SleepDebtMetric(Metric):
    """近七天的**欠账**。

    只累计亏欠，不让"某晚睡了十小时"抵消另一晚的通宵 —— 身体不是账本，
    补觉补不回来的是规律性。缺记录的那几晚不计入（不知道 ≠ 没睡）。
    """

    key = "sleep_debt"
    label = "睡眠债"
    horizon_days = 7

    def compute(self, ctx: Context) -> Signal:
        known = [one for one in ctx.nights[:7] if one.known]
        debt_min = sum(max(0, ctx.target_min - one.minutes) for one in known)
        debt_hours = round(debt_min / 60, 1)
        # 欠满 8 小时就是 0 分：那是连续几晚没睡够的量级
        value = clamp(1.0 - debt_hours / 8.0, 0.0, 1.0)
        if not known:
            note = "还没有睡眠记录"
        elif debt_hours >= 0.5:
            note = f"近七天欠 {debt_hours:g} 小时"
        else:
            note = "近七天没欠觉"
        return Signal(
            key=self.key,
            label=self.label,
            value=value,
            raw={"debtHours": debt_hours, "nights": len(known), "targetMin": ctx.target_min},
            note=note,
        )


class LastNightMetric(Metric):
    """昨晚睡了多久（相对目标）。没有记录时按"不知道"处理，给 0.5 而不是 0。"""

    key = "last_night"
    label = "昨晚"
    horizon_days = 1

    def compute(self, ctx: Context) -> Signal:
        night = next((one for one in ctx.nights if one.known), None)
        if night is None:
            return Signal(
                key=self.key,
                label=self.label,
                value=0.5,
                raw={"minutes": 0, "known": False},
                note="昨晚没打卡",
            )

        minutes = night.minutes
        value = clamp(minutes / max(1, ctx.target_min), 0.0, 1.0)
        hours = f"{minutes // 60}h{minutes % 60:02d}m"
        trend = "够" if minutes >= ctx.target_min else "不足"
        return Signal(
            key=self.key,
            label=self.label,
            value=value,
            raw={
                "minutes": minutes,
                "known": True,
                "bedAt": night.bed_at.isoformat() if night.bed_at else None,
                "wakeAt": night.wake_at.isoformat() if night.wake_at else None,
            },
            note=f"昨晚 {hours}（{trend}）",
        )


class StudyLoadMetric(Metric):
    """今天已经压在学习上的量（相对每日上限）。越满，剩下的精力越少。"""

    key = "study_load"
    label = "今日负荷"
    horizon_days = 0

    def compute(self, ctx: Context) -> Signal:
        cap = max(60, int(ctx.options.get("dailyStudyCapMin") or 300))
        used = max(0, int(ctx.planned_min))
        value = clamp(1.0 - used / cap, 0.0, 1.0)
        return Signal(
            key=self.key,
            label=self.label,
            value=value,
            raw={"plannedMin": used, "capMin": cap},
            note=f"今天已排 {used} 分钟（上限 {cap}）",
        )


class CircadianMetric(Metric):
    """昼夜节律：一条 48 槽的**形状**，不是一个数。

    基础形状来自常识（清晨爬升、午饭后低谷、傍晚次高、深夜塌下去），
    个人的深浅偏好只在两处加减：上午型抬 08–12 点，夜间型抬 19–23 点。
    真正的个性化应该来自长期记录 —— 那是以后加一个 `learned_rhythm` 指标的事，
    不该把这张表调成玄学。
    """

    key = "circadian"
    label = "昼夜节律"
    horizon_days = 0

    #: (小时, 值)，相邻点之间线性插值；24 点收回夜间水平
    _BASE: tuple[tuple[float, float], ...] = (
        (0.0, 0.14),
        (5.0, 0.14),
        (7.0, 0.45),
        (8.5, 0.78),
        (9.5, 0.86),
        (11.5, 0.74),
        (13.0, 0.46),
        (14.5, 0.56),
        (16.5, 0.74),
        (18.0, 0.58),
        (19.5, 0.72),
        (21.5, 0.62),
        (23.0, 0.38),
        (24.0, 0.14),
    )

    @classmethod
    def _at(cls, hour: float) -> float:
        points = cls._BASE
        for index in range(1, len(points)):
            left_h, left_v = points[index - 1]
            right_h, right_v = points[index]
            if hour <= right_h:
                span = max(1e-6, right_h - left_h)
                ratio = clamp((hour - left_h) / span, 0.0, 1.0)
                return left_v + (right_v - left_v) * ratio
        return points[-1][1]

    def compute(self, ctx: Context) -> Signal:
        pref = str(ctx.deep_pref or "morning").lower()
        shape: list[float] = []
        for index in range(SLOTS):
            hour = index * 0.5 + 0.25
            value = self._at(hour)
            if pref in ("morning", "both") and 8.0 <= hour < 12.0:
                value += 0.08
            if pref in ("evening", "both") and 19.0 <= hour < 23.0:
                value += 0.08
            shape.append(round(clamp(value, 0.05, 1.0), 3))

        return Signal(
            key=self.key,
            label=self.label,
            value=round(sum(shape) / len(shape), 3),
            slot=tuple(shape),
            raw={"deepPref": pref},
            note=f"节律基线（偏好：{pref}）",
        )


def register_builtin_metrics() -> None:
    for metric in (SleepDebtMetric(), LastNightMetric(), StudyLoadMetric(), CircadianMetric()):
        register_metric(metric)


__all__ = [
    "CircadianMetric",
    "LastNightMetric",
    "SleepDebtMetric",
    "StudyLoadMetric",
    "register_builtin_metrics",
]
