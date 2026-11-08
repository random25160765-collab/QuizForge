"""健康模块的数据库那一半：打卡、档位查表、今天的汇总。

**这里不放任何"算法"**：精力怎么算在 `profiles.py`，什么算信号在 `metrics.py`，
今天该怎么对自己在 `advisories.py`。这一层只做三件事：

1. 把库里的记录攒成 `Context`（纯数据）；
2. 按注册表把 Metric → Profile → Advisory 跑一遍；
3. 把结果拼成接口要的形状，或者写回一条睡眠记录。

这样换一套指标 / 档位，不用动这里的任何一行。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy import or_, select

from ..models import HealthRegime, ScheduleEvent, SleepLog
from .base import ADVISORIES, KIND_CHAT, METRICS, PROFILES, Context, Night, Signal

#: 设置里 `health` 那一块的默认值。缺什么补什么，不让"没配过"变成"关掉了"。
DEFAULT_OPTIONS: dict[str, Any] = {
    "profile": "standard",
    "targetBedtime": "23:30",
    "targetWake": "07:00",
    "deepPref": "morning",
    "curfew": True,
    "graceMin": 30,
    "dailyStudyCapMin": 300,
    "gateDefaults": {"art": "recall", "science": "quiz", "skill": "artifact", "other": ""},
}


def local_now() -> datetime:
    """本机当前时刻（带本地时区）。

    为什么不用 UTC：作息是**墙上时钟**的事（23:30 睡），不是绝对时刻的事。
    睡眠记录落库仍然是 UTC（`UtcDateTime`），进出各转一次。
    """
    return datetime.now().astimezone()


def options_of(db) -> dict:  # noqa: ANN001
    """读设置里的 `health` 块，补齐默认值。"""
    from .. import settings_store  # noqa: PLC0415 —— 避免循环导入

    stored = (settings_store.load(db) or {}).get("health")
    merged = dict(DEFAULT_OPTIONS)
    if isinstance(stored, dict):
        merged.update({key: value for key, value in stored.items() if value is not None})
    return merged


def _parse_clock(raw: Any, fallback: time) -> time:
    try:
        hour, minute = str(raw).split(":", 1)
        return time(int(hour), int(minute))
    except (TypeError, ValueError):
        return fallback


def target_window(options: dict) -> tuple[time, time, int]:
    """目标作息 → (入睡时刻, 起床时刻, 目标分钟)。

    跨午夜（23:30–07:00）是常态，所以分钟数要按"跨没跨天"算。
    """
    bed = _parse_clock(options.get("targetBedtime"), time(23, 30))
    wake = _parse_clock(options.get("targetWake"), time(7, 0))
    bed_min = bed.hour * 60 + bed.minute
    wake_min = wake.hour * 60 + wake.minute
    span = wake_min - bed_min
    if span <= 0:
        span += 24 * 60
    return bed, wake, span


def _to_local(moment: datetime | None, tz) -> datetime | None:  # noqa: ANN001
    if moment is None:
        return None
    return moment.astimezone(tz) if moment.tzinfo else moment.replace(tzinfo=UTC).astimezone(tz)


def nights_of(db, now: datetime, limit: int = 14) -> tuple[Night, ...]:  # noqa: ANN001
    """最近若干晚，按"最后发生的那一刻"倒序 —— 昨晚排在最前。"""
    rows = db.scalars(
        select(SleepLog)
        .order_by(SleepLog.wake_at.desc().nullslast(), SleepLog.bed_at.desc())
        .limit(limit)
    ).all()
    out: list[Night] = []
    for row in rows:
        if row.bed_at is None and row.wake_at is None:
            continue
        out.append(
            Night(
                bed_at=_to_local(row.bed_at, now.tzinfo),
                wake_at=_to_local(row.wake_at, now.tzinfo),
                quality=int(row.quality or 0),
                source=row.source or "manual",
            )
        )
    out.sort(key=lambda one: (one.wake_at or one.bed_at or now), reverse=True)
    return tuple(out)


def regime_for(db, day: date) -> HealthRegime | None:  # noqa: ANN001
    """覆盖这一天的档位；有重叠时**开始日最晚**的那条赢（最新的判断优先）。"""
    return db.scalars(
        select(HealthRegime)
        .where(HealthRegime.start_date <= day)
        .where(or_(HealthRegime.end_date.is_(None), HealthRegime.end_date >= day))
        .order_by(HealthRegime.start_date.desc(), HealthRegime.created_at.desc())
        .limit(1)
    ).first()


def _day_bounds(day: date, tz) -> tuple[datetime, datetime]:  # noqa: ANN001
    start = datetime.combine(day, time(0, 0), tzinfo=tz)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


def _today_load(db, day: date, tz) -> tuple[int, int]:  # noqa: ANN001
    """(今天已排的学习分钟, 今天的课时长)。课表块 `sub_id` 非空，计划块 `item_id` 非空。

    **求解器自己排的块不算负荷**（`run_id` 非空）：它们是"打算做的事"，
    不是"已经做掉的量"。算进去的话，一次重排会让精力曲线变差、下一次重排
    再把块挪走 —— 越重排越乱，而且撤销也对不上。
    """
    start, end = _day_bounds(day, tz)
    rows = db.scalars(
        select(ScheduleEvent).where(ScheduleEvent.start_at < end, ScheduleEvent.end_at > start)
    ).all()
    planned = 0
    classes = 0
    for row in rows:
        minutes = max(0, int((row.end_at - row.start_at).total_seconds() // 60))
        if row.sub_id:
            classes += minutes
        elif row.item_id and not row.run_id:
            planned += minutes
    return planned, classes


def build_context(db, now: datetime | None = None) -> Context:  # noqa: ANN001
    now = now or local_now()
    options = options_of(db)
    bed, wake, target_min = target_window(options)
    regime = regime_for(db, now.date())

    profile_key = str(options.get("profile") or "standard")
    params: dict = {}
    regime_info = None
    if regime is not None:
        profile_key = regime.profile or profile_key
        params = dict(regime.params or {})
        regime_info = {
            "id": regime.id,
            "title": regime.title,
            "profile": regime.profile,
            "startDate": regime.start_date.isoformat(),
            "endDate": regime.end_date.isoformat() if regime.end_date else None,
        }
    if profile_key not in PROFILES:
        # 档位被删掉了（或从别的机器同步来的名字这台机器没有）：退回标准档，
        # 但**说清楚**，不要安静地按一套不存在的策略算
        profile_key = "standard"

    planned, classes = _today_load(db, now.date(), now.tzinfo)
    return Context(
        now=now,
        today=now.date(),
        nights=nights_of(db, now),
        target_min=target_min,
        target_bed=bed,
        target_wake=wake,
        deep_pref=str(options.get("deepPref") or "morning"),
        planned_min=planned,
        class_min=classes,
        profile_key=profile_key,
        regime=regime_info,
        params=params,
        options=options,
    )


def evaluate(db, now: datetime | None = None) -> tuple[Context, dict[str, Signal], Any, list]:  # noqa: ANN001, ANN401
    """跑一遍注册表：Context → 信号 → 曲线 → 建议。四处都按**注册顺序**来。"""
    ctx = build_context(db, now)
    signals = {key: metric.compute(ctx) for key, metric in METRICS.items()}
    profile = PROFILES[ctx.profile_key]
    curve = profile.curve(ctx, signals)
    advice = []
    for advisory in ADVISORIES.values():
        advice.extend(advisory.advise(ctx, signals, curve))
    return ctx, signals, curve, advice


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment else None


def _sleep_json(row: SleepLog) -> dict:
    minutes = 0
    if row.bed_at and row.wake_at:
        minutes = max(0, int((row.wake_at - row.bed_at).total_seconds() // 60))
    return {
        "id": row.id,
        "bedAt": _iso(row.bed_at),
        "wakeAt": _iso(row.wake_at),
        "minutes": minutes,
        "quality": int(row.quality or 0),
        "source": row.source or "manual",
        "note": row.note or "",
    }


def open_session(db) -> SleepLog | None:  # noqa: ANN001
    """还没合上的那一条：按了"睡了"但没按"起床"。"""
    return db.scalars(
        select(SleepLog)
        .where(SleepLog.bed_at.is_not(None), SleepLog.wake_at.is_(None))
        .order_by(SleepLog.bed_at.desc())
        .limit(1)
    ).first()


def start_sleep(db, now: datetime | None = None, at: datetime | None = None) -> SleepLog:  # noqa: ANN001
    """「睡了」：开一条记录。

    已经有一条开口的就**改它的时刻**而不是再开一条 —— 那种情况基本是
    "刚才手滑按早了"或"昨晚忘了按起床"，两条开口记录只会让统计更乱。
    """
    now = now or local_now()
    moment = at or now
    row = open_session(db)
    if row is not None:
        row.bed_at = moment
    else:
        row = SleepLog(bed_at=moment, source="manual")
        db.add(row)
    db.commit()
    db.refresh(row)
    return row


def wake_up(db, now: datetime | None = None, at: datetime | None = None) -> SleepLog:  # noqa: ANN001
    """「起床了」：把最近那条开口的合上；没有开口的就单记一个醒来时刻。"""
    now = now or local_now()
    moment = at or now
    row = open_session(db)
    if row is None:
        row = SleepLog(wake_at=moment, source="manual")
        db.add(row)
    else:
        row.wake_at = moment
    db.commit()
    db.refresh(row)
    return row


def last_sleep(db) -> SleepLog | None:  # noqa: ANN001
    return db.scalars(
        select(SleepLog).order_by(SleepLog.wake_at.desc().nullslast(), SleepLog.bed_at.desc()).limit(1)
    ).first()


def sleep_logs(db, days: int = 30) -> list[dict]:
    since = datetime.now(UTC) - timedelta(days=max(1, min(365, days)))
    rows = db.scalars(
        select(SleepLog)
        .where(or_(SleepLog.wake_at >= since, SleepLog.bed_at >= since))
        .order_by(SleepLog.bed_at.desc().nullslast())
    ).all()
    return [_sleep_json(row) for row in rows]


def today(db, now: datetime | None = None) -> dict:  # noqa: ANN001
    """`GET /api/wellbeing/today` 的全部内容。"""
    ctx, signals, curve, advice = evaluate(db, now)
    open_row = open_session(db)
    last = last_sleep(db)
    night = next((one for one in ctx.nights if one.known), None)

    return {
        "now": _iso(ctx.now),
        "today": ctx.today.isoformat(),
        "profile": ctx.profile_key,
        "profileLabel": PROFILES[ctx.profile_key].label,
        "regime": ctx.regime,
        "targets": {
            "bed": ctx.target_bed.strftime("%H:%M"),
            "wake": ctx.target_wake.strftime("%H:%M"),
            "sleepMin": ctx.target_min,
        },
        "sleep": {
            "lastNightMin": night.minutes if night else 0,
            "bedAt": _iso(night.bed_at) if night else None,
            "wakeAt": _iso(night.wake_at) if night else None,
            "debtHours": (signals.get("sleep_debt").raw or {}).get("debtHours", 0)
            if signals.get("sleep_debt")
            else 0,
            "nights": len([one for one in ctx.nights if one.known]),
        },
        "signals": [
            {
                "key": one.key,
                "label": one.label,
                "value": round(one.value, 3),
                "note": one.note,
                "raw": one.raw,
            }
            for one in signals.values()
        ],
        "curve": {
            "slots": [round(value, 3) for value in curve.slots],
            "deepBudgetMin": curve.deep_budget_min,
            "headline": curve.headline,
            "reasons": list(curve.reasons),
        },
        "advice": [one.as_dict() for one in advice],
        "active": _sleep_json(open_row) if open_row is not None else None,
        "lastSleep": _sleep_json(last) if last is not None else None,
    }


def chat_lines(db, now: datetime | None = None) -> list[str]:  # noqa: ANN001
    """对话侧要听的那些话（只读 `chat_intervention`，不认"睡觉"这个具体事由）。"""
    _, _, _, advice = evaluate(db, now)
    return [one.text for one in advice if one.kind == KIND_CHAT]


__all__ = [
    "DEFAULT_OPTIONS",
    "build_context",
    "chat_lines",
    "evaluate",
    "last_sleep",
    "local_now",
    "nights_of",
    "open_session",
    "options_of",
    "regime_for",
    "sleep_logs",
    "start_sleep",
    "target_window",
    "today",
    "wake_up",
]
