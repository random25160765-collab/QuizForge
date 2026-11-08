"""计划与日程的读写。

## 为什么是两组资源、不是一组

**计划是计划，日程是日程**（用户原话）。前者是"要做什么"，可以一直没有日期；
后者是"哪个时段做什么"，必须落在时间轴上。所以这里三张表、三组接口，
外加一组**日历订阅**（iCalendar）：

    /api/plans                 计划（连带它的条目）
    /api/plan-items            单条待办（改日期、勾完成、删）
    /api/schedule              时间块（可按时间窗查）
    /api/schedule/subscriptions 日历订阅（按 RFC 5545 拉一份课表/会议进来）

条目带 `dueDate` **不等于**它进了日程 —— 那只是"这天该做"。要占时段，
得在日程页把它拖进日历，或者由对话里的 `propose_schedule` 提案。

路径刻意拆成三段前缀而不是 `/plans/{id}/items`：静态段与路径参数混在一起时，
FastAPI 的匹配顺序要靠人的记性，而 `/plan-items/xxx` 不会跟 `/plans/xxx` 撞。

## 幂等

对话里的卡片会带一个 `proposalId`。它是**唯一索引**：同一张卡片点两次、
断网重试、两个标签页同时点，都只会落到同一份计划上（`created: false`）。
没有它的话，"确认"这个动作就会因为一次网络抖动建出两份一模一样的计划。

## 删除为什么在应用层显式做

不用 `ON DELETE CASCADE`：跨机同步是按**行**记日志的，级联删掉的子行不会
出现在日志里，于是别的机器上会留下孤儿行。这里显式删子行，每一行都有删除记录。
"""

from __future__ import annotations

from datetime import UTC, date as date_type, datetime, timedelta
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from .. import ics, schedule_view
from ..deps import DbSession
from ..models import CalendarSubscription, Plan, PlanItem, ScheduleEvent

router = APIRouter(prefix="/api", tags=["plans"])


# ============================================================================ 小工具


def _iso(value: datetime | None) -> str | None:
    """时刻一律给 UTC 的 ISO 串（`Z` 结尾）。前端 `new Date(...)` 能直接认。"""
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _day(value: date_type | None) -> str | None:
    return value.isoformat() if value else None


def _parse_day(raw) -> date_type | None:  # noqa: ANN001
    """`2026-11-08` → date。空串 / None → None（= 还没排日子）。"""
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return date_type.fromisoformat(text[:10])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"这不是一个日期：{text}") from exc


def _parse_at(raw, field: str) -> datetime:  # noqa: ANN001
    """ISO 时刻 → **带时区的 UTC**。

    没有时区标记的输入按 UTC 解释（前端总会带偏移，这条只是兜底）——
    不按本地时区猜：服务端的本地时区与用户那台机器无关。
    """
    text = str(raw or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail=f"{field} 不能为空")
    try:
        one = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{field} 不是合法时刻：{text}") from exc
    return one.replace(tzinfo=UTC) if one.tzinfo is None else one.astimezone(UTC)


#: 专注度与学科是**闭集**：拼错一个词（"Deep"/"深度"）会让排程当成没指定，
#: 与其静默降级，不如在这里钉住取值，前端与对话都按这两组词来。
FOCUS_LEVELS = ("light", "medium", "deep")
DOMAINS = ("art", "science", "skill", "other")


def _focus(raw, fallback: str = "medium") -> str:  # noqa: ANN001
    text = str(raw or "").strip().lower()
    return text if text in FOCUS_LEVELS else fallback


def _domain(raw, fallback: str = "other") -> str:  # noqa: ANN001
    text = str(raw or "").strip().lower()
    return text if text in DOMAINS else fallback


def _points(raw) -> list[str]:  # noqa: ANN001
    """关联的知识点 key：去空、去重、限量。存成 JSON 列表（不建关联表）。"""
    if not isinstance(raw, list):
        return []
    seen: list[str] = []
    for one in raw:
        key = str(one or "").strip()[:120]
        if key and key not in seen:
            seen.append(key)
    return seen[:20]


def _item_json(item: PlanItem) -> dict:
    return {
        "id": item.id,
        "planId": item.plan_id,
        "title": item.title,
        "note": item.note,
        "dueDate": _day(item.due_date),
        "important": bool(item.important),
        "estimateMin": int(item.estimate_min or 0),
        # 自动排程用的元数据（老条目读出来是默认值，行为与以前一致）
        "focus": item.focus or "medium",
        "domain": item.domain or "other",
        "splittable": bool(item.splittable),
        "maxChunkMin": int(item.max_chunk_min or 0),
        "earliestDate": _day(item.earliest_date),
        "gate": item.gate or "",
        "points": list(item.points or []),
        "done": bool(item.done),
        "doneAt": _iso(item.done_at),
        "sort": int(item.sort or 0),
    }


def _item_ref(db, item_id):  # noqa: ANN001, ANN202
    """`(条目标题, 所属计划 id)` —— 事件行自己的标题可能与条目不同（排期时改过），
    所以界面要这两个字段来画"来自哪条待办"的标签。"""
    if not item_id:
        return "", ""
    row = db.execute(
        select(PlanItem.title, PlanItem.plan_id).where(PlanItem.id == item_id)
    ).first()
    return (row[0], row[1]) if row else ("", "")


def _int(raw, default: int = 0) -> int:  # noqa: ANN001
    """容忍"字符串数字 / null / 垃圾"这三种输入 —— 前端把输入框的值直接送上来。"""
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _plan_json(plan: Plan, items: list[PlanItem]) -> dict:
    return {
        "id": plan.id,
        "proposalId": plan.proposal_id,
        "title": plan.title,
        "note": plan.note,
        "status": plan.status,
        "source": plan.source,
        "createdAt": _iso(plan.created_at),
        "updatedAt": _iso(plan.updated_at),
        "items": [_item_json(one) for one in items],
    }


def _items_of(db, plan_ids: list[str]) -> dict[str, list[PlanItem]]:  # noqa: ANN001
    """一次取回多份计划的条目，避免 N+1（列表页一屏就是十几份计划）。"""
    if not plan_ids:
        return {}
    rows = db.scalars(
        select(PlanItem)
        .where(PlanItem.plan_id.in_(plan_ids))
        .order_by(PlanItem.sort, PlanItem.created_at)
    ).all()
    out: dict[str, list[PlanItem]] = {pid: [] for pid in plan_ids}
    for row in rows:
        out.setdefault(row.plan_id, []).append(row)
    return out


# ============================================================================ 计划


@router.get("/plans")
def list_plans(db: DbSession, status: str = "", proposalId: str = "") -> dict:
    """计划清单（含条目）。

    `proposalId` 是给**凭条回读**用的：卡片点过确认之后，界面靠它问一句
    "这份计划到底建成没有"，而不是自己记一个"我点过了"的本地布尔 ——
    后者在刷新之后就没了，而记录不会。
    """
    query = select(Plan)
    if status:
        query = query.where(Plan.status == status)
    if proposalId:
        query = query.where(Plan.proposal_id == proposalId)
    plans = db.scalars(query.order_by(Plan.created_at.desc(), Plan.id)).all()
    buckets = _items_of(db, [one.id for one in plans])
    payload = [_plan_json(one, buckets.get(one.id, [])) for one in plans]
    out: dict = {"plans": payload}
    if proposalId:
        out["plan"] = payload[0] if payload else None
    return out


@router.post("/plans")
def create_plan(body: dict, db: DbSession) -> dict:
    """新建一份计划（对话里点"加入"走的就是这里）。

    `proposalId` 已存在时**直接返回既有的那份**（`created: false`）：
    重复点、断网重试、两个标签页一起点，都只该有一份。
    """
    proposal_id = str(body.get("proposalId") or "").strip() or None
    if proposal_id:
        existing = db.scalar(select(Plan).where(Plan.proposal_id == proposal_id))
        if existing is not None:
            return {
                "plan": _plan_json(existing, _items_of(db, [existing.id]).get(existing.id, [])),
                "created": False,
            }

    title = str(body.get("title") or "").strip()[:200] or "未命名计划"
    plan = Plan(
        proposal_id=proposal_id,
        title=title,
        note=str(body.get("note") or "")[:2000],
        status="active",
        source=str(body.get("source") or "manual")[:16] or "manual",
    )
    db.add(plan)
    db.flush()  # 拿到 id 再挂条目（不用等 commit）

    for index, raw in enumerate(body.get("items") or []):
        if not isinstance(raw, dict):
            continue
        item_title = str(raw.get("title") or "").strip()[:300]
        if not item_title:
            continue
        db.add(
            PlanItem(
                plan_id=plan.id,
                title=item_title,
                note=str(raw.get("note") or "")[:2000],
                due_date=_parse_day(raw.get("dueDate")),
                important=bool(raw.get("important")),
                estimate_min=max(0, _int(raw.get("estimateMin"))),
                focus=_focus(raw.get("focus")),
                domain=_domain(raw.get("domain")),
                splittable=bool(raw.get("splittable", True)),
                max_chunk_min=max(0, min(240, _int(raw.get("maxChunkMin")))),
                earliest_date=_parse_day(raw.get("earliestDate")),
                gate=str(raw.get("gate") or "").strip()[:32],
                points=_points(raw.get("points")),
                sort=_int(raw.get("sort"), index),
            )
        )

    try:
        db.commit()
    except IntegrityError:
        # 并发：另一个请求刚刚用同一个 proposalId 建好了。回滚后读它。
        db.rollback()
        if proposal_id:
            existing = db.scalar(select(Plan).where(Plan.proposal_id == proposal_id))
            if existing is not None:
                return {
                    "plan": _plan_json(
                        existing, _items_of(db, [existing.id]).get(existing.id, [])
                    ),
                    "created": False,
                }
        raise

    db.refresh(plan)
    return {
        "plan": _plan_json(plan, _items_of(db, [plan.id]).get(plan.id, [])),
        "created": True,
    }


@router.patch("/plans/{plan_id}")
def update_plan(plan_id: str, body: dict, db: DbSession) -> dict:
    plan = db.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="没有这份计划")
    if "title" in body:
        plan.title = str(body.get("title") or "").strip()[:200] or plan.title
    if "note" in body:
        plan.note = str(body.get("note") or "")[:2000]
    if "status" in body:
        status = str(body.get("status") or "").strip()
        if status not in ("active", "archived"):
            raise HTTPException(status_code=400, detail="status 只认 active / archived")
        plan.status = status
    db.commit()
    db.refresh(plan)
    return {"plan": _plan_json(plan, _items_of(db, [plan.id]).get(plan.id, []))}


@router.delete("/plans/{plan_id}")
def delete_plan(plan_id: str, db: DbSession) -> dict:
    """删计划 —— 连带它的条目与**这些条目排出来的时间块**。

    为什么连带删时间块：那些块的存在理由就是"做这条待办"。留下它们，
    日历上会出现一堆没有出处的块。手动建的事件（`item_id` 为空）不受影响。
    """
    plan = db.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="没有这份计划")
    item_ids = db.scalars(select(PlanItem.id).where(PlanItem.plan_id == plan_id)).all()
    removed_events = 0
    if item_ids:
        removed_events = db.execute(
            delete(ScheduleEvent).where(ScheduleEvent.item_id.in_(list(item_ids)))
        ).rowcount or 0
        db.execute(delete(PlanItem).where(PlanItem.plan_id == plan_id))
    db.delete(plan)
    db.commit()
    return {"ok": True, "deleted": {"items": len(item_ids), "events": removed_events}}


# ============================================================================ 条目


@router.post("/plan-items")
def create_item(body: dict, db: DbSession) -> dict:
    plan_id = str(body.get("planId") or "").strip()
    if not plan_id or db.get(Plan, plan_id) is None:
        raise HTTPException(status_code=400, detail="planId 不存在")
    title = str(body.get("title") or "").strip()[:300]
    if not title:
        raise HTTPException(status_code=400, detail="条目要有标题")
    # 追加到末尾：`sort` 缺省取当前最大值 +1，不是 0 —— 否则新条目会插到最前面，
    # 而"我在往清单后面加一条"是绝大多数情况。
    last = db.scalar(select(func.max(PlanItem.sort)).where(PlanItem.plan_id == plan_id))
    item = PlanItem(
        plan_id=plan_id,
        title=title,
        note=str(body.get("note") or "")[:2000],
        due_date=_parse_day(body.get("dueDate")),
        important=bool(body.get("important")),
        estimate_min=max(0, _int(body.get("estimateMin"))),
        focus=_focus(body.get("focus")),
        domain=_domain(body.get("domain")),
        splittable=bool(body.get("splittable", True)),
        max_chunk_min=max(0, min(240, _int(body.get("maxChunkMin")))),
        earliest_date=_parse_day(body.get("earliestDate")),
        gate=str(body.get("gate") or "").strip()[:32],
        points=_points(body.get("points")),
        sort=_int(body.get("sort"), (last or 0) + 1),
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return {"item": _item_json(item)}


@router.patch("/plan-items/{item_id}")
def update_item(item_id: str, body: dict, db: DbSession) -> dict:
    item = db.get(PlanItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="没有这条条目")
    if "title" in body:
        title = str(body.get("title") or "").strip()[:300]
        if title:
            item.title = title
    if "note" in body:
        item.note = str(body.get("note") or "")[:2000]
    if "dueDate" in body:
        # 显式传 null / "" 就是**清掉日期**（回到"还没排日子"），与"没传这个字段"不同
        item.due_date = _parse_day(body.get("dueDate"))
    if "important" in body:
        item.important = bool(body.get("important"))
    if "estimateMin" in body:
        item.estimate_min = max(0, _int(body.get("estimateMin")))
    if "sort" in body:
        item.sort = _int(body.get("sort"))
    # 排程元数据：只改传上来的那几个键（"没传" ≠ "清空"）
    if "focus" in body:
        item.focus = _focus(body.get("focus"))
    if "domain" in body:
        item.domain = _domain(body.get("domain"))
    if "splittable" in body:
        item.splittable = bool(body.get("splittable"))
    if "maxChunkMin" in body:
        item.max_chunk_min = max(0, min(240, _int(body.get("maxChunkMin"))))
    if "earliestDate" in body:
        item.earliest_date = _parse_day(body.get("earliestDate"))
    if "gate" in body:
        item.gate = str(body.get("gate") or "").strip()[:32]
    if "points" in body:
        item.points = _points(body.get("points"))
    if "done" in body:
        done = bool(body.get("done"))
        item.done = done
        item.done_at = datetime.now(UTC) if done else None
    db.commit()
    db.refresh(item)
    return {"item": _item_json(item)}


@router.delete("/plan-items/{item_id}")
def delete_item(item_id: str, db: DbSession) -> dict:
    item = db.get(PlanItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="没有这条条目")
    removed = db.execute(
        delete(ScheduleEvent).where(ScheduleEvent.item_id == item_id)
    ).rowcount or 0
    db.delete(item)
    db.commit()
    return {"ok": True, "deleted": {"events": removed}}


# ============================================================================ 日程


def _event_json(event: ScheduleEvent, item_title: str = "", plan_id: str = "") -> dict:
    return {
        "id": event.id,
        "itemId": event.item_id,
        "itemTitle": item_title or "",
        "planId": plan_id or "",
        # 三种来源在界面上要分开画：订阅来的（课表）/ 计划排的 / 手动建的
        "subId": event.sub_id,
        "title": event.title,
        "note": event.note,
        "location": event.location,
        "startAt": _iso(event.start_at),
        "endAt": _iso(event.end_at),
        "allDay": bool(event.all_day),
        "detached": bool(event.detached),
        # 自动排程要区分"这块谁说了算"：钉住的（人不许动）、上一轮排的（可回收）
        "locked": bool(event.locked),
        "runId": event.run_id,
    }


def _range_events(db, start: datetime, end: datetime) -> list[dict]:  # noqa: ANN001
    """时间窗内的块。用**区间重叠**而不是"起点落在窗内"：

    22:00 开始、跨过午夜的那一块，起点在昨天，但它今天仍然占着两个小时。
    """
    rows = db.execute(
        select(ScheduleEvent, PlanItem.title, PlanItem.plan_id)
        .outerjoin(PlanItem, ScheduleEvent.item_id == PlanItem.id)
        .where(ScheduleEvent.start_at < end, ScheduleEvent.end_at > start)
        .order_by(ScheduleEvent.start_at, ScheduleEvent.id)
    ).all()
    return [_event_json(event, title or "", plan_id or "") for event, title, plan_id in rows]


@router.get("/schedule")
def list_events(
    db: DbSession,
    start: str = Query("", alias="from"),
    end: str = Query("", alias="to"),
) -> dict:
    """按时间窗取事件。前端一次只问当前看得见的那一屏（日 = 1 天，周 = 7 天）。"""
    begin = _parse_at(start, "from")
    finish = _parse_at(end, "to")
    if finish <= begin:
        raise HTTPException(status_code=400, detail="to 必须晚于 from")
    return {
        "events": _range_events(db, begin, finish),
        "from": _iso(begin),
        "to": _iso(finish),
    }


@router.post("/schedule")
def create_event(body: dict, db: DbSession) -> dict:
    proposal_id = str(body.get("proposalId") or "").strip() or None
    if proposal_id:
        # 与 `POST /plans` 同一条规矩：这张排期卡点过了就不要再排一块。
        existing = db.scalar(
            select(ScheduleEvent).where(ScheduleEvent.proposal_id == proposal_id)
        )
        if existing is not None:
            return {
                "event": _event_json(existing, *_item_ref(db, existing.item_id)),
                "created": False,
            }

    start = _parse_at(body.get("startAt"), "startAt")
    end = _parse_at(body.get("endAt"), "endAt")
    if end <= start:
        raise HTTPException(status_code=400, detail="结束时间必须晚于开始时间")
    item_id = str(body.get("itemId") or "").strip() or None
    if item_id and db.get(PlanItem, item_id) is None:
        raise HTTPException(status_code=400, detail="itemId 不存在")
    event = ScheduleEvent(
        proposal_id=proposal_id,
        item_id=item_id,
        title=str(body.get("title") or "").strip()[:300] or "未命名",
        note=str(body.get("note") or "")[:2000],
        # 地点在块上直接显示（教室就在眼前，不该非要悬停才看得见）
        location=str(body.get("location") or "").strip()[:200],
        start_at=start,
        end_at=end,
        all_day=bool(body.get("allDay")),
    )
    db.add(event)
    try:
        db.commit()
    except IntegrityError:
        # 并发：另一个请求刚用同一个 proposalId 排好了 —— 读它，别报错。
        db.rollback()
        existing = db.scalar(
            select(ScheduleEvent).where(ScheduleEvent.proposal_id == proposal_id)
        )
        if existing is None:
            raise
        return {
            "event": _event_json(existing, *_item_ref(db, existing.item_id)),
            "created": False,
        }
    db.refresh(event)
    return {
        "event": _event_json(event, *_item_ref(db, event.item_id)),
        "created": True,
        "conflicts": schedule_view.conflicts_of(db, event.start_at, event.end_at, exclude=[event.id]),
    }


@router.patch("/schedule/{event_id}")
def update_event(event_id: str, body: dict, db: DbSession) -> dict:
    event = db.get(ScheduleEvent, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="没有这个时间块")
    if "title" in body:
        title = str(body.get("title") or "").strip()[:300]
        if title:
            event.title = title
    if "note" in body:
        event.note = str(body.get("note") or "")[:2000]
    if "location" in body:
        event.location = str(body.get("location") or "").strip()[:200]
    if "startAt" in body:
        event.start_at = _parse_at(body.get("startAt"), "startAt")
    if "endAt" in body:
        event.end_at = _parse_at(body.get("endAt"), "endAt")
    if "allDay" in body:
        event.all_day = bool(body.get("allDay"))
    if "itemId" in body:
        item_id = str(body.get("itemId") or "").strip() or None
        if item_id and db.get(PlanItem, item_id) is None:
            raise HTTPException(status_code=400, detail="itemId 不存在")
        event.item_id = item_id
    if "locked" in body:
        event.locked = bool(body.get("locked"))
    if event.end_at <= event.start_at:
        raise HTTPException(status_code=400, detail="结束时间必须晚于开始时间")
    if any(key in body for key in ("title", "note", "location", "startAt", "endAt", "allDay")):
        # 用户动过的块就是**事实**：订阅同步不许盖回去，重排也不许回收（见
        # `scheduler.recyclable`）。钉住（`locked`）与"动过"是两件事：前者是
        # "别动我"，后者是"这一块已经是我的了"。
        event.detached = True
    db.commit()
    db.refresh(event)
    return {
        "event": _event_json(event, *_item_ref(db, event.item_id)),
        # 改完可能撞上别的块：不拦（有时候是故意的），但要说出来
        "conflicts": schedule_view.conflicts_of(db, event.start_at, event.end_at, exclude=[event.id]),
    }


@router.delete("/schedule/{event_id}")
def delete_event(event_id: str, db: DbSession) -> dict:
    event = db.get(ScheduleEvent, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="没有这个时间块")
    db.delete(event)
    db.commit()
    return {"ok": True}


# ============================================================================ 订阅
#
# "我们的日历需要支持标准协议"—— 所以这一组只认 iCalendar（RFC 5545），
# 解析与展开在 `app/ics.py`，这里只负责**落库**与对账。
#
# 为什么是"订阅"而不是"一次性导入"：课表会调课、会补课。一次性导入之后，
# 用户只能每周手工再导一遍。订阅 = 一个地址 + 一个"同步"动作，
# 重新同步时按 `(uid, occurrence)` 对上，多的删、变的改、少的补。


def _mask_url(url: str) -> str:
    """地址里的**查询值**抹掉再给界面看。

    课表的订阅地址后面就是一串 JWT：拿到地址等于拿到这个人的整个课表。
    界面只需要认得"是哪个地址"，不需要那把钥匙。
    """
    parts = urlsplit(url)
    if not parts.query:
        return url
    keys = [chunk.split("=", 1)[0] for chunk in parts.query.split("&") if chunk]
    return f"{parts.scheme}://{parts.netloc}{parts.path}?" + "&".join(f"{key}=…" for key in keys)


def _sub_json(sub: CalendarSubscription, count: int = 0) -> dict:
    return {
        "id": sub.id,
        "title": sub.title,
        "url": _mask_url(sub.url),
        "host": urlsplit(sub.url).netloc,
        "enabled": bool(sub.enabled),
        "events": int(count or 0),
        "lastSyncAt": _iso(sub.last_sync_at),
        "lastResult": sub.last_result,
    }


def _as_utc(value: datetime) -> datetime:
    """库里读出来的时刻可能是裸的（SQLite），比大小之前先钉成 UTC。"""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _apply_calendar(db, sub: CalendarSubscription, cal: ics.Calendar) -> dict:  # noqa: ANN001
    """把一份日历**对上**库里已有的块：多的删、变的改、少的补。

    对齐键是 `(uid, occurrence)`，不是标题或时间 —— 课表里同名同时间的两节课是有的，
    而用户改过的那一块也还记得自己原来是谁（`detached`）。
    """
    horizon = datetime.now(UTC) + timedelta(days=730)
    existing = db.scalars(
        select(ScheduleEvent).where(ScheduleEvent.sub_id == sub.id)
    ).all()
    index = {(row.uid, row.occurrence): row for row in existing}

    seen: set[tuple[str, str]] = set()
    created = updated = pinned = 0
    full = False
    for one in cal.events:
        if full:
            break
        for start, end, key in one.occurrences(horizon):
            ident = (one.uid, key)
            if ident in seen:
                continue
            if len(seen) >= 5000:
                # 一份订阅 5000 块已经多到不像日历了；宁可截断，也别把库塞爆
                full = True
                break
            seen.add(ident)
            start_utc, end_utc = start.astimezone(UTC), end.astimezone(UTC)
            row = index.get(ident)
            if row is None:
                db.add(
                    ScheduleEvent(
                        sub_id=sub.id,
                        uid=one.uid,
                        occurrence=key,
                        title=one.summary,
                        note=one.description,
                        location=one.location,
                        start_at=start_utc,
                        end_at=end_utc,
                        all_day=one.all_day,
                    )
                )
                created += 1
                continue
            if row.detached:
                pinned += 1
                continue
            changed = (
                row.title != one.summary
                or row.note != one.description
                or row.location != one.location
                or bool(row.all_day) != bool(one.all_day)
                or _as_utc(row.start_at) != start_utc
                or _as_utc(row.end_at) != end_utc
            )
            if changed:
                row.title = one.summary
                row.note = one.description
                row.location = one.location
                row.start_at = start_utc
                row.end_at = end_utc
                row.all_day = one.all_day
                updated += 1

    removed = 0
    for ident, row in index.items():
        if ident in seen or row.detached:
            continue
        db.delete(row)
        removed += 1

    sub.last_sync_at = datetime.now(UTC)
    sub.last_result = (
        f"已同步 {len(seen)} 块（新增 {created} · 更新 {updated} · 删除 {removed}）"
        + (f" · 你改过的 {pinned} 块没动" if pinned else "")
    )
    return {
        "total": len(seen),
        # 不叫 `created`：外层响应里那个 `created` 表示"这份订阅是不是新建的"，
        # 两件事同名会互相覆盖（第一次写就踩了）
        "added": created,
        "updated": updated,
        "removed": removed,
        "pinned": pinned,
    }


@router.get("/schedule/subscriptions")
def list_subscriptions(db: DbSession) -> dict:
    rows = db.scalars(
        select(CalendarSubscription).order_by(
            CalendarSubscription.created_at, CalendarSubscription.id
        )
    ).all()
    counts = dict(
        db.execute(
            select(ScheduleEvent.sub_id, func.count())
            .where(ScheduleEvent.sub_id.is_not(None))
            .group_by(ScheduleEvent.sub_id)
        ).all()
    )
    # `url` 是脱敏过的：前端只显示"从哪儿订的"，不拿那把钥匙
    return {"subscriptions": [_sub_json(one, counts.get(one.id, 0)) for one in rows]}


@router.post("/schedule/subscriptions")
def subscribe(body: dict, db: DbSession) -> dict:
    """订阅一个 iCalendar 地址，并**当场**同步一次。

    同一个地址再提交一次不会多出一份：找到既有那行，在原地上重新同步。
    """
    url = str(body.get("url") or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="订阅地址不能为空")
    if len(url) > 2000:
        raise HTTPException(status_code=400, detail="订阅地址太长了")

    try:
        text = ics.fetch_feed(url)
    except ics.FeedError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    cal = ics.parse(text)
    if not cal.events:
        raise HTTPException(status_code=400, detail="这份日历里没有事件（或者格式不认识）")

    sub = db.scalar(select(CalendarSubscription).where(CalendarSubscription.url == url))
    fresh = sub is None
    if fresh:
        sub = CalendarSubscription(url=url)
        db.add(sub)
        db.flush()

    title = str(body.get("title") or "").strip()[:200]
    sub.title = title or cal.name or urlsplit(url).netloc
    stats = _apply_calendar(db, sub, cal)
    db.commit()
    db.refresh(sub)
    return {"subscription": _sub_json(sub, stats["total"]), "created": fresh, **stats}


@router.post("/schedule/subscriptions/{sub_id}/sync")
def sync_subscription(sub_id: str, db: DbSession) -> dict:
    sub = db.get(CalendarSubscription, sub_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="没有这份订阅")
    try:
        text = ics.fetch_feed(sub.url)
    except ics.FeedError as exc:
        # 同步失败**不清空**已有的块：学校服务器抽风时，日历不该跟着空掉
        sub.last_result = "同步失败：" + str(exc)[:200]
        db.commit()
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    cal = ics.parse(text)
    if not cal.events:
        raise HTTPException(status_code=400, detail="这次拉回来的日历里没有事件")
    stats = _apply_calendar(db, sub, cal)
    db.commit()
    db.refresh(sub)
    return {"subscription": _sub_json(sub, stats["total"]), **stats}


@router.delete("/schedule/subscriptions/{sub_id}")
def unsubscribe(sub_id: str, db: DbSession) -> dict:
    """退订：把它同步进来的块一起删掉（那些块的存在理由就是这份订阅）。"""
    sub = db.get(CalendarSubscription, sub_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="没有这份订阅")
    removed = db.execute(
        delete(ScheduleEvent).where(ScheduleEvent.sub_id == sub_id)
    ).rowcount or 0
    db.delete(sub)
    db.commit()
    return {"ok": True, "deleted": {"events": removed}}
