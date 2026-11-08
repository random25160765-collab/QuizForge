"""内置精力档位：把指标组合成一条曲线 + 深度预算。

档位是**策略**：权重、阈值都在 `defaults` 里，档位（regime）可以按日期区间覆盖。
所以"这半个月病后恢复" 不需要改代码，只需要在库里加一条 `profile="recovery"`。

新增档位请照这里的形状写并在 `wellbeing/__init__.py` 注册；如果新档位只是
"更保守一点"，继承 `StandardProfile` 改几个默认值就够了。
"""

from __future__ import annotations

from .base import SLOTS, Context, EnergyCurve, Profile, Signal, clamp, register_profile


class StandardProfile(Profile):
    """标准档：睡眠债压整天的上限，昨晚的睡眠压深度预算，已排负荷再压一档。"""

    key = "standard"
    label = "标准"

    defaults = {
        #: 睡饱时一天能排多少分钟深度用脑
        "deepBudgetBaseMin": 240,
        #: 再糟也不会低于这个（留一点"逼自己开始"的余地）
        "deepBudgetFloorMin": 60,
        #: 深度任务要求的最低精力（求解器的 energy_floor）
        "deepEnergyFloor": 0.6,
        "sleepWeight": 0.55,
        "nightWeight": 0.25,
        "loadWeight": 0.20,
        #: 每上 10 小时课，全天再压这么多（课本身也是消耗）
        "classPenalty": 0.15,
        "minEnergy": 0.12,
        #: 曲线整体缩放（恢复期用）
        "scale": 1.0,
        #: 深度预算系数
        "deepFactor": 1.0,
    }

    def params_for(self, ctx: Context) -> dict:
        return {**self.defaults, **(ctx.params or {})}

    def curve(self, ctx: Context, signals: dict[str, Signal]) -> EnergyCurve:
        p = self.params_for(ctx)
        sleep = signals.get("sleep_debt")
        night = signals.get("last_night")
        load = signals.get("study_load")
        rhythm = signals.get("circadian")

        sleep_v = sleep.value if sleep else 0.5
        night_v = night.value if night else 0.5
        load_v = load.value if load else 1.0

        weight = p["sleepWeight"] + p["nightWeight"] + p["loadWeight"]
        vigour = (
            sleep_v * p["sleepWeight"] + night_v * p["nightWeight"] + load_v * p["loadWeight"]
        ) / max(1e-6, weight)

        shape = rhythm.slot if rhythm and rhythm.slot else tuple([0.6] * SLOTS)
        # 上课是当天的固定消耗：按课时长打折，只在白天减
        class_pen = clamp(ctx.class_min / 600.0, 0.0, 1.0) * p["classPenalty"]
        ceiling = 0.55 + 0.45 * vigour
        slots: list[float] = []
        for index, base in enumerate(shape):
            hour = index * 0.5
            daytime = 1.0 if 8.0 <= hour < 22.0 else 0.0
            value = base * ceiling * p["scale"] - class_pen * daytime
            slots.append(round(clamp(value, p["minEnergy"], 1.0), 3))

        budget = (
            p["deepBudgetBaseMin"]
            * (0.45 + 0.55 * sleep_v)
            * (0.60 + 0.40 * night_v)
            * (0.70 + 0.30 * load_v)
            * p["deepFactor"]
        )
        budget = int(round(clamp(budget, p["deepBudgetFloorMin"], p["deepBudgetBaseMin"]) / 10) * 10)

        reasons: list[str] = []
        if sleep:
            reasons.append(f"睡眠债：{sleep.note}")
        if night:
            reasons.append(f"{night.note}")
        if load:
            reasons.append(f"{load.note}")
        if ctx.class_min:
            reasons.append(f"今天有 {ctx.class_min // 60}h{ctx.class_min % 60:02d}m 课")
        if ctx.regime:
            reasons.append(f"档位：{ctx.regime.get('title') or ctx.profile_key}")

        return EnergyCurve(
            slots=slots,
            deep_budget_min=budget,
            headline=self.headline(budget, vigour),
            reasons=reasons,
        )

    def headline(self, budget: int, vigour: float) -> str:
        level = "高" if vigour >= 0.75 else "中" if vigour >= 0.5 else "低"
        return f"精力{level} · 深度预算 {budget // 60}h{budget % 60:02d}m"


class RecoveryProfile(StandardProfile):
    """恢复期：整体压低、深度预算砍到三分之一、宁可少排也不硬撑。"""

    key = "recovery"
    label = "恢复期"

    defaults = {
        **StandardProfile.defaults,
        "deepBudgetBaseMin": 150,
        "deepBudgetFloorMin": 30,
        "deepEnergyFloor": 0.7,
        "scale": 0.62,
        "deepFactor": 0.35,
        "minEnergy": 0.1,
    }

    def headline(self, budget: int, vigour: float) -> str:
        return f"恢复期 · 深度预算 {budget // 60}h{budget % 60:02d}m，能少排就少排"


def register_builtin_profiles() -> None:
    for profile in (StandardProfile(), RecoveryProfile()):
        register_profile(profile)


__all__ = ["RecoveryProfile", "StandardProfile", "register_builtin_profiles"]
