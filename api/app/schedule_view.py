"""日程的两种「给外面看」的换算：**本地时刻**与**撞车检测**。

## 为什么单独一层

这两件事被三处同时需要：对话工具（`tools.py` 的 `list_schedule` /
`propose_schedule`）、HTTP 接口（`routers/plans.py` 的新建与改期响应）、
以及界面上任何「把时刻说给人听」的地方。

各写一份的代价已经付过一次：工具一律回 UTC，模型手里只有 UTC，于是它要么
自己按 +8 推（推对了是运气，推错了就是把课排到凌晨），要么干脆说不清几点。
**换算只能有一份实现**，而且必须和界面用的是同一份。

## 两条约定

* **不带偏移的时刻按本地时间理解**。模型脑子里想的是"他那边九点"，
  按 UTC 理解会整整错开一个时区；本机就是他的机器，本地时区没有歧义。
* **冲突是提示，不是拦截**。同一段时间被两块占着，有时是有意的
  （自习 + 旁听）。这里只负责把"撞了谁"摆出来，让模型开口说一句，
  拦不拦由调用方决定。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, tzinfo

from sqlalchemy import select

from .models import ScheduleEvent

#: 本地时区名（`CST` 这种缩写有歧义，只当参考，真正能算数的是偏移）
WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def local_tz() -> tzinfo:
    """本机时区。

    本机就是用户的机器（local-first），所以"服务器时区"没有第二义。
    用 `astimezone()` 拿的是**当前**这一刻的偏移；中国不实行夏令时，
    跨季节也不会错。真有夏令时的地区，这一处要换成 `zoneinfo` + 明确的区名。
    """
    return datetime.now().astimezone().tzinfo or UTC


def iso_local(value: datetime | None) -> str:
    """UTC 时刻 → 本地 ISO（带偏移，例如 `2026-11-09T14:00:00+08:00`）。"""
    if value is None:
        return ""
    return value.astimezone(local_tz()).isoformat(timespec="seconds")


def tz_brief(now: datetime | None = None) -> dict:
    """给模型看的"现在几点、在哪"。每份日程相关的结果都带一份。"""
    moment = now or datetime.now(UTC)
    local = moment.astimezone(local_tz())
    offset = local.utcoffset() or timedelta(0)
    minutes = int(offset.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    minutes = abs(minutes)
    return {
        "offset": f"{sign}{minutes // 60:02d}:{minutes % 60:02d}",
        "now": local.isoformat(timespec="seconds"),
        "date": local.date().isoformat(),
        "weekday": WEEKDAYS[local.weekday()],
    }


def _brief(row: ScheduleEvent) -> dict:
    return {
        "id": row.id,
        "title": row.title,
        "startAt": row.start_at.isoformat(),
        "endAt": row.end_at.isoformat(),
        "startLocal": iso_local(row.start_at),
        "endLocal": iso_local(row.end_at),
        "location": row.location or "",
        "subId": row.sub_id or "",
        "itemId": row.item_id or "",
    }


def conflicts_of(
    db, start: datetime, end: datetime, exclude: list[str] | tuple[str, ...] = ()  # noqa: ANN001
) -> list[dict]:
    """这段时间里已经占着的块（重叠即算，含订阅与手动建的）。

    `exclude` 给"改期中的那一块自己"：它当然和自己重叠。
    """
    skip = {str(one) for one in exclude if one}
    rows = db.scalars(
        select(ScheduleEvent)
        .where(ScheduleEvent.start_at < end, ScheduleEvent.end_at > start)
        .order_by(ScheduleEvent.start_at, ScheduleEvent.id)
    ).all()
    return [_brief(row) for row in rows if row.id not in skip]
