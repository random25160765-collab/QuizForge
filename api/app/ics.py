"""iCalendar（RFC 5545）读进来，展开成日程上的时间块。

用户的原话是"我们的日历需要支持标准协议"。所以这里刻意**不认识"课表"**：
输入是 VCALENDAR，输出是"什么时候、做什么、在哪"的一串块 —— 课表、会议邀请、
节假日订阅走的都是同一条路。

只做**读**。每一处取舍都写在旁边：

* 折行（RFC 5545 §3.1：续行以一个空格或制表符开头）必须先把行拼回去，否则
  `SUMMARY` 会被腰斩 —— 这是手写解析器最先踩的坑；
* 时间三种写法都认：`...Z`（UTC）、`TZID=Asia/Shanghai`、以及**不带时区**的浮动
  时间（按日历自己的 `X-WR-TIMEZONE`；没有就按 UTC —— 不拿服务器本地时区去猜，
  那会让同一份订阅在两台机器上落成不同的时间）；
* 重复用 RRULE 展开成一个个实例：FREQ 只做 DAILY / WEEKLY / MONTHLY（BYDAY 含
  `2TU` 这种序数），INTERVAL / COUNT / UNTIL / EXDATE 全认；其它频率只当一次性
  —— 宁可少展开，也不要凭空造出和订阅方想法不一样的块；
* 展开到 `horizon` 为止，并给每次展开设上限：订阅地址可能是无限重复的，而日历
  只需要看得见的那一段。

为什么自己写而不装 `icalendar` / `recurring-ical-events`：这两个包要联网装，
而这里要的子集很小；把"认什么、不认什么"写清楚，比装一个包再裁它更好维护。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

#: 订阅内容的上限。一份课表几百 KB，5MB 已经大到不像是日历了。
MAX_BYTES = 5 * 1024 * 1024
#: 一条 VEVENT 最多展开多少块（每周一次，够撑十年）。防的是"没有 UNTIL 的无限重复"。
MAX_OCCURRENCES = 500


class FeedError(Exception):
    """订阅地址那边的问题：拉不下来、不像日历、太大。"""


# ============================================================================ 拉取


def fetch_feed(url: str) -> str:
    """把订阅地址拉下来。只认 http/https；20 秒超时；跟随跳转。

    单独一个函数是为了**可测**：用例把它换掉，就不必真的去连学校服务器。
    """
    # `webcal://` 是日历订阅的惯用写法（Google/Apple 都用它），实际拉取走 https
    raw = str(url or "").strip()
    if raw.lower().startswith("webcal://"):
        raw = "https://" + raw[len("webcal://") :]
    if not re.match(r"^https?://", raw, re.I):
        raise FeedError("订阅地址要是 http / https 开头（webcal:// 会被当成 https 处理）")

    import httpx  # noqa: PLC0415 —— 只有这条路径需要它

    try:
        with httpx.Client(
            timeout=20.0,
            follow_redirects=True,
            headers={"User-Agent": "quizforge-calendar/1.0"},
        ) as client:
            resp = client.get(raw)
    except Exception as exc:  # noqa: BLE001 —— httpx 的异常层级换过版本，一律当成"连不上"
        raise FeedError("订阅地址没连上：" + str(exc)[:120]) from exc

    if resp.status_code != 200:
        raise FeedError(f"订阅地址回了 HTTP {resp.status_code}")
    if len(resp.content) > MAX_BYTES:
        raise FeedError("订阅内容超过 5MB，不像一份日历")
    if "BEGIN:VCALENDAR" not in resp.text[:4096].upper():
        raise FeedError("这个地址给的不是 iCalendar（没看到 BEGIN:VCALENDAR）")
    return resp.text


# ============================================================================ 词法


def _unfold(text: str) -> list[str]:
    """折行先拼回去（RFC 5545 §3.1）。不拼的话长标题会被腰斩。"""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    for line in lines:
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def _split_unquoted(text: str, sep: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    quoted = False
    for ch in text:
        if ch == '"':
            quoted = not quoted
            buf.append(ch)
        elif ch == sep and not quoted:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return parts


def _split_property(line: str) -> tuple[str, dict[str, str], str]:
    """`NAME;A=B;C="x:y":VALUE` → `("NAME", {"A": "B", "C": "x:y"}, "VALUE")`。

    冒号要先跳过引号里的那几个 —— `TZID="Asia/Shanghai"` 不会带冒号，但
    `ALTREP="http://..."` 会，硬按冒号切就会把值切掉一半。
    """
    head_end = -1
    quoted = False
    for index, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
        elif ch == ":" and not quoted:
            head_end = index
            break
    if head_end < 0:
        return "", {}, ""

    head, value = line[:head_end], line[head_end + 1 :]
    chunks = _split_unquoted(head, ";")
    params: dict[str, str] = {}
    for chunk in chunks[1:]:
        if "=" not in chunk:
            continue
        key, raw = chunk.split("=", 1)
        params[key.strip().upper()] = raw.strip().strip('"')
    return chunks[0].strip().upper(), params, value


_ESCAPES = {"n": "\n", "N": "\n", ",": ",", ";": ";", "\\": "\\"}


def _unescape(value: str) -> str:
    out: list[str] = []
    index = 0
    while index < len(value):
        ch = value[index]
        if ch == "\\" and index + 1 < len(value):
            nxt = value[index + 1]
            out.append(_ESCAPES.get(nxt, nxt))
            index += 2
        else:
            out.append(ch)
            index += 1
    return "".join(out)


def _zone(name: str | None) -> ZoneInfo | None:
    if not name:
        return None
    try:
        return ZoneInfo(str(name).strip())
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return None


# ============================================================================ 值


def _parse_dt(
    value: str, params: dict[str, str], default_zone: ZoneInfo | None
) -> tuple[datetime | None, bool]:
    """`DTSTART` 那一串 → (时刻, 是否全天)。认不出的返回 None，**不猜**。"""
    raw = str(value or "").strip()
    if not raw:
        return None, False

    all_day = params.get("VALUE", "").upper() == "DATE" or bool(re.fullmatch(r"\d{8}", raw))
    zone = _zone(params.get("TZID")) or default_zone or UTC
    if all_day:
        try:
            day = datetime.strptime(raw[:8], "%Y%m%d")
        except ValueError:
            return None, True
        return day.replace(tzinfo=zone), True

    body = raw.upper()
    is_utc = body.endswith("Z")
    if is_utc:
        body = body[:-1]
    fmt = "%Y%m%dT%H%M%S" if len(body) >= 15 else "%Y%m%dT%H%M"
    try:
        naive = datetime.strptime(body, fmt)
    except ValueError:
        return None, False
    return naive.replace(tzinfo=UTC if is_utc else zone), False


_DURATION = re.compile(
    r"^P(?:(?P<weeks>\d+)W)?(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$",
    re.I,
)


def _parse_duration(raw: str) -> timedelta | None:
    match = _DURATION.match(str(raw or "").strip())
    if not match:
        return None
    parts = {key: int(value or 0) for key, value in match.groupdict().items()}
    return timedelta(
        weeks=parts["weeks"],
        days=parts["days"],
        hours=parts["hours"],
        minutes=parts["minutes"],
        seconds=parts["seconds"],
    )


def _parse_until(raw: str | None, zone: ZoneInfo | None) -> datetime | None:
    text = str(raw or "").strip().upper()
    if not text:
        return None
    one, _all_day = _parse_dt(text, {}, zone)
    return one.astimezone(UTC) if one else None


def _int(raw, default: int = 0) -> int:  # noqa: ANN001
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


# ============================================================================ 展开


def occurrence_key(start: datetime) -> str:
    """这一块在订阅里的**稳定身份**：原始开始时刻的 UTC 紧凑写法。

    重新同步时靠 `(uid, occurrence)` 认"还是那一块"。没有它就只能按标题+时间猜，
    而课表里同名同时间的两节课是有的。
    """
    return start.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


_WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


def _by_days(raw: str) -> list[tuple[int, int]]:
    """`MO,2TU,-1FR` → `[(0, 0), (1, 2), (4, -1)]`（星期, 第几个；0 = 每个）。"""
    out: list[tuple[int, int]] = []
    for chunk in str(raw or "").upper().split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        suffix = chunk[-2:]
        if suffix not in _WEEKDAYS:
            continue
        prefix = chunk[:-2].strip()
        out.append((_WEEKDAYS[suffix], _int(prefix, 0) if prefix else 0))
    return out


def _add_months(day: date, months: int) -> date:
    month = day.month - 1 + months
    year = day.year + month // 12
    month = month % 12 + 1
    return date(year, month, 1)


def _month_days(month_start: date, weekday: int, nth: int) -> list[date]:
    """某个月里符合 `BYDAY` 的那几天。`nth` 为 0 = 这个月所有的该星期几。"""
    days_in_month = (
        _add_months(month_start, 1) - timedelta(days=1)
    ).day
    hits = [
        month_start + timedelta(days=offset)
        for offset in range(days_in_month)
        if (month_start + timedelta(days=offset)).weekday() == weekday
    ]
    if nth == 0:
        return hits
    index = nth - 1 if nth > 0 else len(hits) + nth
    return [hits[index]] if 0 <= index < len(hits) else []


@dataclass(frozen=True)
class VEvent:
    """一条 ICS 事件（还没展开）。时间都是**带时区**的。"""

    uid: str
    summary: str
    description: str
    location: str
    start: datetime
    end: datetime
    all_day: bool
    rrule: dict[str, str] = field(default_factory=dict)
    exdates: tuple[datetime, ...] = ()

    def occurrences(
        self, horizon: datetime, limit: int = MAX_OCCURRENCES
    ) -> list[tuple[datetime, datetime, str]]:
        """展开成 `[(开始, 结束, 身份), …]`，到 `horizon` 为止。

        `horizon` 是 UTC 时刻：订阅可能是无限重复的，日历只要看得见的那一段。
        """
        span = self.end - self.start
        if span.total_seconds() <= 0:
            span = timedelta(days=1) if self.all_day else timedelta(hours=1)

        excluded = set(self.exdates)
        out: list[tuple[datetime, datetime, str]] = []

        def push(start: datetime) -> None:
            if start not in excluded:
                out.append((start, start + span, occurrence_key(start)))

        if not self.rrule:
            if self.start.astimezone(UTC) <= horizon:
                push(self.start)
            return out

        rule = self.rrule
        freq = str(rule.get("FREQ") or "").upper()
        interval = max(1, _int(rule.get("INTERVAL"), 1))
        count = max(0, _int(rule.get("COUNT"), 0))
        until = _parse_until(rule.get("UNTIL"), self.start.tzinfo)
        days = _by_days(rule.get("BYDAY", ""))

        produced = 0
        for moment in self._series(freq, interval, days):
            # 先看边界：超过 horizon / UNTIL 就停（生成顺序是递增的）
            if moment.astimezone(UTC) > horizon:
                break
            if until and moment.astimezone(UTC) > until:
                break
            produced += 1
            # COUNT 数的是"规则生成了几次"，被 EXDATE 排掉的那次也算
            if count and produced > count:
                break
            push(moment)
            if len(out) >= limit:
                break
        return out

    def _series(
        self, freq: str, interval: int, days: list[tuple[int, int]]
    ) -> Iterator[datetime]:
        """按频率吐出"理论上应该发生"的时刻（含被 EXDATE 排掉的）。"""
        clock = self.start.timetz()

        if freq == "DAILY":
            moment = self.start
            # 上限只是兜底：真正的终止条件在调用方（horizon / UNTIL / COUNT）
            for _ in range(MAX_OCCURRENCES * 4):
                yield moment
                moment = moment + timedelta(days=interval)
            return

        if freq == "WEEKLY":
            weekdays = sorted({weekday for weekday, _nth in days}) or [self.start.weekday()]
            monday = self.start.date() - timedelta(days=self.start.weekday())
            for step in range(MAX_OCCURRENCES * 4):
                week = monday + timedelta(weeks=step * interval)
                for weekday in weekdays:
                    moment = datetime.combine(week + timedelta(days=weekday), clock)
                    if moment >= self.start:
                        yield moment
            return

        if freq == "MONTHLY":
            month = date(self.start.year, self.start.month, 1)
            for step in range(MAX_OCCURRENCES * 4):
                base = _add_months(month, step * interval)
                if days:
                    picked: list[date] = []
                    for weekday, nth in days:
                        picked.extend(_month_days(base, weekday, nth))
                    picked.sort()
                else:
                    # 没写 BYDAY 就是"每月同一天"。那个月没有 31 号就没有这一次
                    #（RFC 5545 的原意），不能悄悄挪到 28 号。
                    last_day = (_add_months(base, 1) - timedelta(days=1)).day
                    picked = (
                        [base.replace(day=self.start.day)]
                        if self.start.day <= last_day
                        else []
                    )
                for day in picked:
                    moment = datetime.combine(day, clock)
                    if moment >= self.start:
                        yield moment
            return

        # 其它频率（YEARLY 等）：只当一次性 —— 宁可少展开，也不凭空造块
        yield self.start


# ============================================================================ 解析


@dataclass(frozen=True)
class Calendar:
    name: str
    timezone: str
    events: list[VEvent]


def parse(text: str) -> Calendar:
    """整份 VCALENDAR → `Calendar`。坏事件跳过，不因为一条脏数据丢掉整份课表。"""
    lines = _unfold(text)
    raw_events: list[list[tuple[str, dict[str, str], str]]] = []
    name = ""
    tzname = ""
    stack: list[str] = []
    current: list[tuple[str, dict[str, str], str]] | None = None

    for line in lines:
        if not line.strip():
            continue
        key, params, value = _split_property(line)
        if key == "BEGIN":
            stack.append(value.strip().upper())
            if stack[-1] == "VEVENT":
                current = []
            continue
        if key == "END":
            kind = stack.pop() if stack else ""
            if kind == "VEVENT" and current is not None:
                raw_events.append(current)
                current = None
            continue
        if not stack:
            continue
        if stack[-1] == "VEVENT":
            # 只收 VEVENT 的**直接**属性：嵌在里面的 VALARM 有自己的 SUMMARY，别串味
            if current is not None:
                current.append((key, params, value))
        elif stack == ["VCALENDAR"]:
            if key == "X-WR-CALNAME":
                name = _unescape(value).strip()
            elif key in ("X-WR-TIMEZONE", "TIMEZONE-ID"):
                tzname = value.strip()

    zone = _zone(tzname) or UTC
    events = [one for one in (_to_event(props, zone) for props in raw_events) if one]
    return Calendar(name=name, timezone=tzname, events=events)


def _to_event(
    props: list[tuple[str, dict[str, str], str]], zone: ZoneInfo
) -> VEvent | None:
    bag: dict[str, tuple[dict[str, str], str]] = {}
    for key, params, value in props:
        # 同名属性取第一个：RRULE/EXDATE 是例外，下面单独拼
        if key in ("EXDATE", "RDATE"):
            continue
        bag.setdefault(key, (params, value))

    status = bag.get("STATUS", ({}, ""))[1].strip().upper()
    if status == "CANCELLED":
        # 取消的那一条不是"看不见"，是"别排" —— 整条丢掉，展开也没意义
        return None

    start_raw = bag.get("DTSTART")
    if not start_raw:
        return None
    start, all_day = _parse_dt(start_raw[1], start_raw[0], zone)
    if start is None:
        return None

    end: datetime | None = None
    if "DTEND" in bag:
        end, _ = _parse_dt(bag["DTEND"][1], bag["DTEND"][0], zone)
    elif "DURATION" in bag:
        span = _parse_duration(bag["DURATION"][1])
        if span is not None:
            end = start + span
    if end is None:
        end = start + (timedelta(days=1) if all_day else timedelta(hours=1))

    rrule: dict[str, str] = {}
    rule_raw = bag.get("RRULE", ({}, ""))[1]
    for chunk in rule_raw.split(";"):
        if "=" in chunk:
            key, value = chunk.split("=", 1)
            rrule[key.strip().upper()] = value.strip().upper()

    exdates: list[datetime] = []
    for key, params, value in props:
        if key != "EXDATE":
            continue
        for piece in value.split(","):
            one, _ = _parse_dt(piece, params, zone)
            if one is not None:
                exdates.append(one)

    summary = _unescape(bag.get("SUMMARY", ({}, ""))[1]).strip()
    uid = _unescape(bag.get("UID", ({}, ""))[1]).strip()
    if not uid:
        # 没有 UID 的订阅是坏的，但也不该整份丢掉：用内容算一个稳定的身份
        digest = hashlib.sha1(f"{summary}|{start.isoformat()}".encode()).hexdigest()
        uid = "sha1-" + digest[:32]

    return VEvent(
        uid=uid[:200],
        summary=(summary or "(未命名)")[:300],
        description=_unescape(bag.get("DESCRIPTION", ({}, ""))[1]).strip()[:2000],
        location=_unescape(bag.get("LOCATION", ({}, ""))[1]).strip()[:200],
        start=start,
        end=end,
        all_day=bool(all_day),
        rrule=rrule,
        exdates=tuple(exdates),
    )


__all__ = [
    "MAX_BYTES",
    "Calendar",
    "FeedError",
    "VEvent",
    "fetch_feed",
    "occurrence_key",
    "parse",
]
