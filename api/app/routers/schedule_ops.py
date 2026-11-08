"""日程的"操作"接口：健康（wellbeing）与之后的自动排程。

为什么不塞进 `plans.py`：那个文件是"计划 / 日程"的 CRUD，已经四百多行；
重排、撤销、打卡、档位这些是**另一件事**（一次请求里要跑求解器或跑整套指标），
混进去会让"改一条待办"和"重排一周"共享同一段代码。

健康这一半的路由只做三件事：解析请求、调 `app.wellbeing.service`、把结果转 camelCase。
**不在这里算任何精力或阈值** —— 那些在注册表里（见 `app/wellbeing/base.py` 顶部）。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from .. import gates, scheduler, wellbeing
from ..deps import DbSession
from ..models import GateRun, HealthRegime, PlanItem, ScheduleEvent, ScheduleRun, SleepLog

router = APIRouter(prefix="/api", tags=["schedule-ops"])


# ============================================================================ 小工具


def _moment(raw, now: datetime | None = None) -> datetime | None:  # noqa: ANN001
    """ISO 字符串 → 带时区的时刻。不带时区的按本机时区解释（这是个本机应用）。"""
    if raw in (None, ""):
        return None
    try:
        value = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="时间要 ISO 格式，例如 2026-11-08T23:41") from exc
    if value.tzinfo is None:
        value = value.astimezone()
    return value


def _day(raw, fallback: date | None = None) -> date | None:  # noqa: ANN001
    if raw in (None, ""):
        return fallback
    try:
        return date.fromisoformat(str(raw).strip())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="日期要 YYYY-MM-DD") from exc


def _sleep_json(row: SleepLog) -> dict:
    minutes = 0
    if row.bed_at and row.wake_at:
        minutes = max(0, int((row.wake_at - row.bed_at).total_seconds() // 60))
    return {
        "id": row.id,
        "bedAt": row.bed_at.isoformat() if row.bed_at else None,
        "wakeAt": row.wake_at.isoformat() if row.wake_at else None,
        "minutes": minutes,
        "quality": int(row.quality or 0),
        "source": row.source or "manual",
        "note": row.note or "",
    }


def _regime_json(row: HealthRegime) -> dict:
    return {
        "id": row.id,
        "title": row.title,
        "startDate": row.start_date.isoformat(),
        "endDate": row.end_date.isoformat() if row.end_date else None,
        "profile": row.profile,
        "params": dict(row.params or {}),
        "note": row.note or "",
    }


# ============================================================================ 健康：今天


@router.get("/wellbeing/today")
def wellbeing_today(db: DbSession) -> dict:
    """今天的精力曲线、信号、建议与打卡状态（界面与对话共用这一份）。"""
    return wellbeing.today(db)


@router.get("/wellbeing/strategies")
def wellbeing_strategies() -> dict:
    """注册表里现在有哪些指标 / 档位 / 建议 —— 设置页的下拉用它，新增策略会自动出现。"""
    return {
        "metrics": [
            {"key": one.key, "label": one.label} for one in wellbeing.METRICS.values()
        ],
        "profiles": [
            {"key": one.key, "label": one.label} for one in wellbeing.PROFILES.values()
        ],
        "advisories": [
            {"key": one.key, "label": one.label} for one in wellbeing.ADVISORIES.values()
        ],
        "options": wellbeing.DEFAULT_OPTIONS,
    }


# ============================================================================ 健康：打卡


@router.get("/wellbeing/sleep")
def sleep_list(db: DbSession, days: int = Query(30, ge=1, le=365)) -> dict:
    return {"logs": wellbeing.sleep_logs(db, days)}


@router.post("/wellbeing/sleep/start")
def sleep_start(db: DbSession, body: dict | None = None) -> dict:
    row = wellbeing.start_sleep(db, at=_moment((body or {}).get("at")))
    return {"sleep": _sleep_json(row), "today": wellbeing.today(db)}


@router.post("/wellbeing/sleep/wake")
def sleep_wake(db: DbSession, body: dict | None = None) -> dict:
    row = wellbeing.wake_up(db, at=_moment((body or {}).get("at")))
    return {"sleep": _sleep_json(row), "today": wellbeing.today(db)}


@router.patch("/wellbeing/sleep/{sleep_id}")
def sleep_patch(sleep_id: str, body: dict, db: DbSession) -> dict:
    row = db.get(SleepLog, sleep_id)
    if row is None:
        raise HTTPException(status_code=404, detail="没有这条睡眠记录")
    if "bedAt" in body:
        row.bed_at = _moment(body.get("bedAt"))
    if "wakeAt" in body:
        row.wake_at = _moment(body.get("wakeAt"))
    if "quality" in body:
        row.quality = max(0, min(5, int(body.get("quality") or 0)))
    if "note" in body:
        row.note = str(body.get("note") or "")
    if row.bed_at and row.wake_at and row.wake_at <= row.bed_at:
        raise HTTPException(status_code=400, detail="醒来时间必须晚于入睡时间")
    db.commit()
    db.refresh(row)
    return {"sleep": _sleep_json(row), "today": wellbeing.today(db)}


@router.delete("/wellbeing/sleep/{sleep_id}")
def sleep_delete(sleep_id: str, db: DbSession) -> dict:
    row = db.get(SleepLog, sleep_id)
    if row is None:
        raise HTTPException(status_code=404, detail="没有这条睡眠记录")
    db.delete(row)
    db.commit()
    return {"ok": True}


# ============================================================================ 健康：身体状态档位


@router.get("/wellbeing/profiles")
def profile_list(db: DbSession) -> dict:
    """精力档位注册表。

    设置页的下拉、档位时间线的选项都读它 —— **新增档位只注册，不改前端**：
    这里把 `PROFILES` 原样摊出去，界面只认识 `key`/`label`。
    """
    options = wellbeing.options_of(db)
    return {
        "profiles": [
            {"key": one.key, "label": one.label or one.key}
            for one in wellbeing.PROFILES.values()
        ],
        "defaultProfile": options.get("defaultProfile") or "standard",
        "deepPref": options.get("deepPref") or "morning",
        "curfew": {
            "enabled": bool(options.get("curfewEnabled", True)),
            "graceMin": int(options.get("curfewGraceMin") or 0),
        },
        "targets": {
            "bed": options.get("targetBedtime") or "23:00",
            "wake": options.get("targetWake") or "07:00",
        },
        "gateDefaults": dict(options.get("gateDefaults") or {}),
    }


@router.get("/wellbeing/regimes")
def regime_list(db: DbSession) -> dict:
    rows = db.scalars(
        select(HealthRegime).order_by(HealthRegime.start_date.desc(), HealthRegime.created_at.desc())
    ).all()
    return {"regimes": [_regime_json(row) for row in rows]}


@router.post("/wellbeing/regimes")
def regime_create(body: dict, db: DbSession) -> dict:
    title = str(body.get("title") or "").strip()[:100]
    start = _day(body.get("startDate"))
    if start is None:
        raise HTTPException(status_code=400, detail="档位要有开始日期")
    end = _day(body.get("endDate"))
    if end is not None and end < start:
        raise HTTPException(status_code=400, detail="结束日期不能早于开始日期")
    profile = str(body.get("profile") or "standard")
    if profile not in wellbeing.PROFILES:
        raise HTTPException(status_code=400, detail=f"没有这个档位：{profile}")
    row = HealthRegime(
        title=title,
        start_date=start,
        end_date=end,
        profile=profile,
        params=dict(body.get("params") or {}),
        note=str(body.get("note") or ""),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"regime": _regime_json(row), "today": wellbeing.today(db)}


@router.patch("/wellbeing/regimes/{regime_id}")
def regime_patch(regime_id: str, body: dict, db: DbSession) -> dict:
    row = db.get(HealthRegime, regime_id)
    if row is None:
        raise HTTPException(status_code=404, detail="没有这个档位")
    if "title" in body:
        row.title = str(body.get("title") or "").strip()[:100]
    if "startDate" in body:
        start = _day(body.get("startDate"))
        if start is None:
            raise HTTPException(status_code=400, detail="开始日期不能为空")
        row.start_date = start
    if "endDate" in body:
        row.end_date = _day(body.get("endDate"))
    if "profile" in body:
        profile = str(body.get("profile") or "")
        if profile not in wellbeing.PROFILES:
            raise HTTPException(status_code=400, detail=f"没有这个档位：{profile}")
        row.profile = profile
    if "params" in body:
        row.params = dict(body.get("params") or {})
    if "note" in body:
        row.note = str(body.get("note") or "")
    if row.end_date is not None and row.end_date < row.start_date:
        raise HTTPException(status_code=400, detail="结束日期不能早于开始日期")
    db.commit()
    db.refresh(row)
    return {"regime": _regime_json(row), "today": wellbeing.today(db)}


@router.delete("/wellbeing/regimes/{regime_id}")
def regime_delete(regime_id: str, db: DbSession) -> dict:
    row = db.get(HealthRegime, regime_id)
    if row is None:
        raise HTTPException(status_code=404, detail="没有这个档位")
    db.delete(row)
    db.commit()
    return {"ok": True}


# ============================================================================ 重排


#: 撤销时要还原的列。`created_at` 也带上：还原的行该看起来像"从没被动过"。
_SNAPSHOT_FIELDS = (
    "id",
    "proposal_id",
    "item_id",
    "sub_id",
    "uid",
    "occurrence",
    "title",
    "note",
    "location",
    "start_at",
    "end_at",
    "all_day",
    "detached",
    "locked",
    "run_id",
    "created_at",
)
_MOMENT_FIELDS = frozenset({"start_at", "end_at", "created_at"})


def _snapshot(row: ScheduleEvent) -> dict:
    out: dict = {}
    for name in _SNAPSHOT_FIELDS:
        value = getattr(row, name, None)
        out[name] = value.astimezone(UTC).isoformat() if isinstance(value, datetime) else value
    return out


def _from_snapshot(snap: dict) -> ScheduleEvent:
    data: dict = {}
    for name in _SNAPSHOT_FIELDS:
        if name not in snap:
            continue
        value = snap[name]
        if name in _MOMENT_FIELDS and isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        data[name] = value
    return ScheduleEvent(**data)


def _horizon_rows(db, si) -> list[ScheduleEvent]:  # noqa: ANN001
    """求解窗口内的所有块。**只查这一段**：重排不该整库扫描。"""
    begin = datetime.combine(si.start, time(0, 0), tzinfo=si.tz)
    end = begin + timedelta(days=si.days)
    return list(
        db.scalars(
            select(ScheduleEvent).where(
                ScheduleEvent.start_at < end.astimezone(UTC),
                ScheduleEvent.end_at > begin.astimezone(UTC),
            )
        ).all()
    )


def _block_json(block) -> dict:  # noqa: ANN001
    one = block.as_dict()
    return {
        "id": None,
        "itemId": one["taskId"],
        "title": one["title"],
        "startAt": one["start"],
        "endAt": one["end"],
        "focus": one["focus"],
        "domain": one["domain"],
        "reason": one["reason"],
    }


def _diff(old_rows: list[ScheduleEvent], blocks: list) -> dict:
    """这次重排相对"库里现有的块"变了什么。

    身份按 `item_id` 认（一块待办可能拆成好几块）：同一条待办时间集合没变就是
    **没动**（不重写、不换 id，界面也不闪）。变了就整条重建 —— 这样撤销只需
    "删新建 + 按快照写回"，不用去猜哪一块对应哪一块。
    """
    old_by_item: dict[str | None, list[ScheduleEvent]] = defaultdict(list)
    for row in old_rows:
        old_by_item[row.item_id].append(row)
    new_by_item: dict[str | None, list] = defaultdict(list)
    for block in blocks:
        new_by_item[block.task_id].append(block)

    created: list = []
    moved: list = []
    deleted: list[ScheduleEvent] = []
    unchanged = 0
    for item_id in set(old_by_item) | set(new_by_item):
        old = old_by_item.get(item_id, [])
        new = new_by_item.get(item_id, [])
        if not old:
            created.extend(new)
        elif not new:
            deleted.extend(old)
        else:
            old_span = sorted(
                (row.start_at.astimezone(UTC), row.end_at.astimezone(UTC)) for row in old
            )
            new_span = sorted(
                (one.start.astimezone(UTC), one.end.astimezone(UTC)) for one in new
            )
            if old_span == new_span:
                unchanged += len(old)
            else:
                moved.extend(new)
                deleted.extend(old)
    return {
        "created": created,
        "moved": moved,
        "deleted": deleted,
        "unchanged": unchanged,
        "counts": {
            "created": len(created),
            "moved": len(moved),
            "deleted": len(deleted),
            "unchanged": unchanged,
        },
    }


@router.get("/schedule/runs")
def run_list(db: DbSession, limit: int = Query(20, ge=1, le=200)) -> dict:
    rows = db.scalars(select(ScheduleRun).order_by(ScheduleRun.created_at.desc()).limit(limit)).all()
    return {
        "runs": [
            {
                "id": row.id,
                "kind": row.kind,
                "created": len(row.created or []),
                "restored": len(row.before or []),
                "profile": row.profile,
                "regimeId": row.regime_id,
                "undoneAt": row.undone_at.isoformat() if row.undone_at else None,
                "createdAt": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ]
    }


@router.post("/schedule/replan")
def replan(db: DbSession, body: dict | None = None) -> dict:
    """重排未来几天。

    `apply=false`（默认）= **只算不写**：对话里那张整周排程卡就是这个形状，
    用户点了"确认"才带 `apply=true` 再来一次（求解器是确定性的，两次结果一致）。
    """
    body = body or {}
    try:
        days = max(1, min(14, int(body.get("horizonDays") or 7)))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="horizonDays 要是整数") from exc
    apply = bool(body.get("apply", False))

    si = scheduler.build_input(db, days=days)
    solution = scheduler.solve(si)

    rows = _horizon_rows(db, si)
    old_rows = [row for row in rows if scheduler.recyclable(row)]
    plan_diff = _diff(old_rows, solution.blocks)

    payload = {
        "engine": solution.engine,
        "profile": si.profile,
        "regimeId": si.regime_id,
        "horizon": {"from": si.start.isoformat(), "days": si.days},
        "blocks": [_block_json(one) for one in solution.blocks],
        "unscheduled": list(solution.unscheduled),
        "stats": dict(solution.stats),
        "constraints": [one.as_dict() for one in si.constraints],
        "diff": {
            "created": [_block_json(one) for one in plan_diff["created"]],
            "moved": [_block_json(one) for one in plan_diff["moved"]],
            # 删掉的也给原时间：界面要在旧位置留 4 秒虚线幽灵（"这块被挪走了"）
            "deleted": [
                {
                    "id": row.id,
                    "itemId": row.item_id,
                    "title": row.title,
                    "startAt": row.start_at.astimezone(UTC).isoformat(),
                    "endAt": row.end_at.astimezone(UTC).isoformat(),
                }
                for row in plan_diff["deleted"]
            ],
            "counts": plan_diff["counts"],
        },
        "runId": None,
        "applied": False,
    }
    if not apply:
        return payload

    run = ScheduleRun(
        kind="replan",
        profile=si.profile,
        regime_id=si.regime_id,
        before=[_snapshot(row) for row in plan_diff["deleted"]],
    )
    db.add(run)
    db.flush()

    for row in plan_diff["deleted"]:
        db.delete(row)

    # 新建的与"挪过去的"是两拨，各自记 id：界面靠它分别做"淡入"与"旧位置留幽灵"
    buckets: dict[str, list[str]] = {"created": [], "moved": []}
    for bucket in ("created", "moved"):
        for block in plan_diff[bucket]:
            row = ScheduleEvent(
                item_id=block.task_id,
                title=block.title,
                start_at=block.start.astimezone(UTC),
                end_at=block.end.astimezone(UTC),
                run_id=run.id,
            )
            db.add(row)
            db.flush()
            buckets[bucket].append(row.id)

    run.created = buckets["created"] + buckets["moved"]
    db.commit()
    db.refresh(run)

    # 落库之后再把新 id 填回去：界面要拿 id 做高亮与"改这一块"
    for bucket, ids in buckets.items():
        if not ids:
            payload["diff"][bucket] = []
            continue
        rows = db.scalars(select(ScheduleEvent).where(ScheduleEvent.id.in_(ids))).all()
        payload["diff"][bucket] = [
            {
                "id": one.id,
                "itemId": one.item_id,
                "title": one.title,
                "startAt": one.start_at.astimezone(UTC).isoformat(),
                "endAt": one.end_at.astimezone(UTC).isoformat(),
            }
            for one in sorted(rows, key=lambda row: (row.start_at, row.id))
        ]
    payload["runId"] = run.id
    payload["applied"] = True
    return payload


@router.post("/schedule/runs/{run_id}/undo")
def undo_run(run_id: str, db: DbSession) -> dict:
    """撤销一次重排：删掉它新建的块，把被它挪走/删掉的块按快照写回。

    只认这一次动过的行 —— 用户在重排之后手动改的别的东西，一动不动。
    """
    run = db.get(ScheduleRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="没有这次重排")
    if run.undone_at is not None:
        raise HTTPException(status_code=409, detail="这次重排已经撤销过了")

    created_ids = [one for one in (run.created or []) if isinstance(one, str)]
    removed = 0
    if created_ids:
        rows = db.scalars(
            select(ScheduleEvent).where(
                ScheduleEvent.id.in_(created_ids), ScheduleEvent.run_id == run.id
            )
        ).all()
        for row in rows:
            db.delete(row)
            removed += 1

    restored = 0
    for snap in run.before or []:
        if not isinstance(snap, dict) or not snap.get("id"):
            continue
        existing = db.get(ScheduleEvent, snap["id"])
        if existing is not None:
            for name in _SNAPSHOT_FIELDS:
                if name in snap and name != "id":
                    value = snap[name]
                    if name in _MOMENT_FIELDS and isinstance(value, str):
                        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
                    setattr(existing, name, value)
        else:
            db.add(_from_snapshot(snap))
        restored += 1

    run.undone_at = datetime.now(UTC)
    db.commit()
    return {"ok": True, "removed": removed, "restored": restored}


# ============================================================================ 门禁


def _gate_json(row: GateRun) -> dict:
    return {
        "id": row.id,
        "itemId": row.item_id,
        "eventId": row.event_id,
        "gate": row.gate,
        "passed": bool(row.passed),
        "score": round(float(row.score or 0), 3),
        "payload": dict(row.payload or {}),
        "createdAt": row.created_at.isoformat() if row.created_at else None,
    }


def _gate_item(db, body: dict) -> PlanItem:  # noqa: ANN001
    item_id = str(body.get("itemId") or "").strip()
    if not item_id:
        raise HTTPException(status_code=400, detail="要指定 itemId")
    item = db.get(PlanItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="没有这条待办")
    return item


@router.get("/gates")
def gates_list(db: DbSession) -> dict:
    """注册表 + 当前的默认映射（设置页与门禁浮层的下拉都读它）。"""
    return {
        "gates": [
            {"key": one.key, "label": one.label, "kind": one.kind}
            for one in gates.GATES.values()
        ],
        "defaults": dict(wellbeing.options_of(db).get("gateDefaults") or {}),
    }


@router.post("/gates/prepare")
def gate_prepare(body: dict, db: DbSession) -> dict:
    """给这条待办出一张卷子（**不带答案**）。"""
    item = _gate_item(db, body)
    event = db.get(ScheduleEvent, str(body.get("eventId") or "")) if body.get("eventId") else None
    payload = gates.prepare(db, item, event)
    payload["eventId"] = event.id if event is not None else None
    return payload


@router.post("/gates/submit")
def gate_submit(body: dict, db: DbSession) -> dict:
    """判分并落库。通过才把条目勾上；没通过**也记一笔**（下一次重排先补它）。"""
    item = _gate_item(db, body)
    event = db.get(ScheduleEvent, str(body.get("eventId") or "")) if body.get("eventId") else None
    answer = body.get("answer") if isinstance(body.get("answer"), dict) else body
    gate = gates.resolve(db, item)
    result = gates.submit(db, item, event, answer)

    row = GateRun(
        item_id=item.id,
        event_id=event.id if event is not None else None,
        gate=gate.key,
        passed=bool(result.passed),
        score=float(result.score or 0),
        payload=dict(result.payload or {}),
        # 显式给时刻（不用 server_default 的秒级 CURRENT_TIMESTAMP）：
        # "最近一次门禁"要靠它排序，同一秒内的通过与不通过必须分得出先后。
        created_at=datetime.now(UTC),
    )
    db.add(row)
    if result.passed:
        item.done = True
        item.done_at = datetime.now(UTC)
    else:
        item.done = False
        item.done_at = None
    db.commit()
    db.refresh(row)
    return {
        "passed": bool(result.passed),
        "score": round(float(result.score or 0), 3),
        "detail": result.detail,
        "gateRun": _gate_json(row),
        "item": {"id": item.id, "done": bool(item.done)},
    }


@router.get("/gates/history")
def gate_history(
    db: DbSession,
    item_id: str = Query("", alias="itemId"),
    limit: int = Query(20, ge=1, le=200),
) -> dict:
    stmt = select(GateRun).order_by(GateRun.created_at.desc()).limit(limit)
    if item_id:
        stmt = stmt.where(GateRun.item_id == item_id)
    return {"runs": [_gate_json(row) for row in db.scalars(stmt).all()]}
