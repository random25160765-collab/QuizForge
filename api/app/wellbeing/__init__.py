"""健康（wellbeing）模块的入口：注册内建策略，并把服务层摊出来。

用它只该做两件事：

    from app import wellbeing
    wellbeing.today(db)          # 读
    wellbeing.register_metric(MyMetric())   # 扩

判据（测试里钉着）：**不 import 具体实现也能工作** —— 路由、求解器、对话
只碰 `METRICS` / `PROFILES` / `ADVISORIES` 这三个字典与 `service` 的函数。
"""

from __future__ import annotations

from .advisories import register_builtin_advisories
from .base import (
    ADVISORIES,
    KIND_CHAT,
    KIND_CONSTRAINT,
    KIND_UI,
    METRICS,
    PROFILES,
    SLOTS,
    SLOT_MINUTES,
    Advisory,
    Advice,
    Context,
    EnergyCurve,
    Metric,
    Night,
    Profile,
    Signal,
    clamp,
    register_advisory,
    register_metric,
    register_profile,
    slot_of,
)
from .metrics import register_builtin_metrics
from .profiles import register_builtin_profiles

# 内建策略在导入时注册一次。重复 import 不会重复注册（模块只执行一次）。
register_builtin_metrics()
register_builtin_profiles()
register_builtin_advisories()

from .service import (  # noqa: E402 —— 必须在注册之后导入，服务层按注册表取策略
    DEFAULT_OPTIONS,
    build_context,
    chat_lines,
    evaluate,
    last_sleep,
    local_now,
    nights_of,
    open_session,
    options_of,
    regime_for,
    sleep_logs,
    start_sleep,
    target_window,
    today,
    wake_up,
)

__all__ = [
    "ADVISORIES",
    "DEFAULT_OPTIONS",
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
    "build_context",
    "chat_lines",
    "clamp",
    "evaluate",
    "last_sleep",
    "local_now",
    "nights_of",
    "open_session",
    "options_of",
    "regime_for",
    "register_advisory",
    "register_metric",
    "register_profile",
    "sleep_logs",
    "slot_of",
    "start_sleep",
    "target_window",
    "today",
    "wake_up",
]
