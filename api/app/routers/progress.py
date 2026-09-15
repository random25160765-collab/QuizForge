"""学习进度的读写。

本模块只管 HTTP：参数校验、错误码、会话与 CSRF。数据怎么合在
`app/sync_ops.py` 里 —— 那部分逻辑（幂等插入、增量累加、补丁 LWW）
是有跨设备正确性要求的，值得单独读懂与单独测试。

## 载荷契约

    POST /api/progress/sync
    {
      "attempts": [ {id, questionId, at, day, status, score, topicKey, response} ],
      "patches":  { "题目id": {_rev, sm2, note, streak, mastered, flagged} },
      "resets":   { "题目id": {attempts, correct, ...} | null },
      "settings": { ... } | null,
      "settingsRev": 7
    }

`attempts` 是**增量**（每次判分一条），`patches` 是主观状态，
`resets` 是「重新设基线」。三者语义不同，刻意分成三个通道：
混在一起就没法表达「这条是增量、那条是绝对值」。

**没有 `days` 通道。** 每日统计由服务端从流水累加得出，
客户端上传绝对累计值正是跨设备少算的根因。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter
from sqlalchemy import func, select

from .. import sync_ops
from ..deps import AuthenticatedWriter, CurrentUser, DbSession
from ..models import Record, UserSettings

router = APIRouter(prefix="/api/progress", tags=["progress"])


@router.get("")
def snapshot(user: CurrentUser, db: DbSession) -> dict:
    """完整进度快照。

    前端拿到后灌进 localStorage，之后所有读仍是同步的本地读 ——
    这是「保留现有运行时」得以成立的关键。
    """
    data = sync_ops.snapshot(db, user.id)
    settings_row = db.get(UserSettings, user.id)
    # 客户端用它把自己的 rev 抬到不低于服务端（Lamport 式）。
    # 不这么做的话，rev 较小的设备其补丁会被服务端永久静默拒绝 ——
    # 表现为「在这台设备上改了设置/星标，刷新就变回去了」。
    max_rev = (
        db.scalar(select(func.max(Record.client_rev)).where(Record.user_id == user.id)) or 0
    )

    return {
        "records": data["records"],
        "days": data["days"],
        "settings": settings_row.data if settings_row else None,
        "settingsRev": settings_row.client_rev if settings_row else 0,
        "rev": max_rev,
    }


@router.post("/sync")
def sync(payload: dict, user: AuthenticatedWriter, db: DbSession) -> dict:
    """批量提交本地攒下的变动。

    顺序有讲究：**先插流水，再应用补丁**。补丁可能给一道还没有流水的题
    打星标，先跑流水能保证那条记录是由流水创建的（计数齐全、`client_rev` 为 0），
    随后补丁在同一行上叠加，不会被流水覆盖掉。

    整体幂等：同内容重发不产生副作用，所以断网重试是安全的。
    """
    rows, dropped = sync_ops.parse_attempts(payload.get("attempts"))

    # 只对**真正插入成功**的流水累加 —— 重发的行不会出现在结果里
    inserted = sync_ops.insert_attempts(db, user.id, rows)
    sync_ops.bump_records(db, user.id, inserted)
    sync_ops.bump_days(db, user.id, inserted)

    sync_ops.apply_resets(db, user.id, payload.get("resets"))
    rejected = sync_ops.apply_patches(db, user.id, payload.get("patches"))

    # 放在流水之后：种子的准入条件是「该日期还没有任何流水」，
    # 必须先让本批流水落库，否则会把刚答过的今天也算成可覆盖的历史
    seeded = sync_ops.apply_days_seed(db, user.id, payload.get("daysSeed"))

    has_settings = "settings" in payload and payload.get("settings") is not None
    settings_out = None
    if has_settings:
        settings_out = _upsert_settings(
            db, user.id, payload.get("settings"), int(payload.get("settingsRev") or 0)
        )

    db.commit()

    return {
        "ok": True,
        "attemptsAccepted": len(inserted),
        "attemptsDropped": dropped,
        # 只回传被拒绝的补丁，避免每次同步都传全量
        "patchesRejected": rejected,
        "daysSeedAccepted": seeded,
        "settings": settings_out,
    }


@router.post("/reset")
def reset(user: AuthenticatedWriter, db: DbSession) -> dict:
    """清空该用户的学习数据。

    设置保留 —— 它属于「偏好」而不是「进度」。流水一并删除，
    因为整个用户的数据都没了，幂等键也就没有意义。
    """
    sync_ops.wipe(db, user.id)
    db.commit()
    return {"ok": True}


def _upsert_settings(db: DbSession, user_id: uuid.UUID, incoming: dict, rev: int) -> dict:
    """设置整体覆盖，按 rev 的 LWW。

    设置是一份小 JSON，逐字段合并反而会在「用户删掉某个键」时表现怪异；
    整体覆盖符合用户的心智。rev 的跨设备单调性由客户端的 Lamport 递增保证。
    """
    row = db.get(UserSettings, user_id)
    if row is None:
        row = UserSettings(user_id=user_id, data=incoming, client_rev=rev)
        db.add(row)
        db.flush()
        return row.data

    if rev and rev <= row.client_rev:
        return row.data

    row.data = incoming
    row.client_rev = max(rev, row.client_rev)
    db.flush()
    return row.data
