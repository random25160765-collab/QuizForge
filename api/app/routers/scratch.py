"""碎片（Scratchpad）的读写：随手记一句的地方，对标 flomo。

## 为什么是独立的一组接口、独立的一张表

待办（`plan_items`）有完成态与日期，笔记（`notes`）是有路径、有 hash 对账的
文档 —— 碎片两样都不是（见 `models.Scratch` 的注释）。所以它只有四个动作：

    GET    /api/scratch        读一页（默认最近 200 条，**旧 → 新**）
    POST   /api/scratch        记一条
    PATCH  /api/scratch/{id}   改一条
    DELETE /api/scratch/{id}   删一条

没有搜索、没有标签、没有归档 —— 这一页的定位是"想到什么写一句"，
多一个维度就多一次整理，而用户要的正是"不用整理"。

## 为什么读出来是旧 → 新

这一栏的排法是对照 QQ 的聊天记录：最新的在底下，视线落点固定在输入框上方。
倒序交给 SQL（`ORDER BY created_at DESC LIMIT n`）再翻回来 —— 拿"最近 n 条"
只需要这一条查询，正序取前 n 条会拿到最老的那批。

## 幂等：`id` 由前端生成

同一条发送失败重试、两个标签页各点一次，都只该落成一行。做法与 `POST /plans`
的 `proposalId` 同一条规矩：`id` 已经在库里就**直接返回那一行**（`created: false`）。
它同时是跨机器同步的稳定身份（别的表靠自增 `id` + 补一个 `uid`，这里一开始
就用随机串，不必再补第二列）。

## 同步

这张表在 `datapack.BOUNDARY` 里归 `user`，于是自动进 `sync.SYNC_TABLES`；
`PULL_ORDER` 里也有它（首次同步走全量，漏一张就永远到不了新机器）；
正文按 `sync.PRIVATE_TABLES` 与对话同一档**加密**上传。这个文件里没有一行
同步代码 —— 那正是那条通道该有的样子。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .. import scratch_tags
from ..deps import DbSession
from ..models import Scratch

router = APIRouter(prefix="/api/scratch", tags=["scratch"])

#: 一条碎片最多这么长。碎片不是文章：真写到几千字，那该去写笔记
#: （写成 `notes` 那样的文档，或者在对话里让 AI 帮你整理）。
#: 4000 是"两屏还能一眼看完"的上限，不是数据库的限制。
MAX_BODY = 4000

#: 前端生成的 id 形状（`crypto.randomUUID()` 去掉横线就是 32 位十六进制）。
#: 只收这几个字符：它要当主键、要进同步日志的 `pk`，不该带空格或斜杠。
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,32}$")


def _iso(value: datetime | None) -> str | None:
    """时刻一律给 UTC 的 ISO 串（`Z` 结尾），与 `routers/plans.py` 同一口径。"""
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_at(raw: str) -> datetime | None:
    """`before` 游标：ISO 时刻 → 带时区的 UTC。空串 = 不要游标。"""
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        one = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"before 不是合法时刻：{text}") from exc
    return one.replace(tzinfo=UTC) if one.tzinfo is None else one.astimezone(UTC)


def _note_json(note: Scratch) -> dict:
    return {
        "id": note.id,
        "body": note.body,
        "createdAt": _iso(note.created_at),
        "updatedAt": _iso(note.updated_at),
        # 标签从正文里解析（不存第二份，见 `app/scratch_tags.py`）。
        # 前端拿它把 `#` 那几个字画成可以点的胶囊 —— 别在 JS 里再写一套正则。
        "tags": scratch_tags.parse(note.body),
    }


def _clean_body(raw) -> str:  # noqa: ANN001
    """正文：去掉首尾空白，但不碰中间的空行 —— 那可能是刻意的分行。

    空的一条直接拒绝：一次误触的回车不该在记录里留下一条空气泡。
    """
    if not isinstance(raw, str):
        raise HTTPException(status_code=400, detail="body 得是字符串")
    text = raw.strip()
    if not text:
        raise HTTPException(status_code=400, detail="空的一条不留 —— 想说什么？")
    if len(text) > MAX_BODY:
        raise HTTPException(
            status_code=400,
            detail=f"一条最多 {MAX_BODY} 字（这条 {len(text)} 字）—— 再长就写成笔记吧。",
        )
    return text


@router.get("")
def list_notes(
    db: DbSession,
    limit: int = Query(200, ge=1, le=500),
    before: str = Query("", description="只看比这个时刻更早的（翻历史用）"),
    tag: str = Query("", description="只看这个标签（含它的子标签）；空 = 全部"),
) -> dict:
    """最近的一页碎片，**旧 → 新**（见文件头那段说明）。带上左栏那棵标签树。

    * `total` —— **当前筛选下**的条数（界面上那行"N 条"读它）；
    * `all` —— 全部条数（"全部"那一行读它，筛选时它不跟着变小）；
    * `tags` —— 标签树（每个节点带条数，父节点含子节点），**永远按全部碎片算** ——
      否则点进一个标签之后，左栏就只剩这一支了，退不出去。

    `tag` 的匹配走 Python：标签在正文里，没有可查的列（见 `app/scratch_tags.py`）。
    碎片是短句，几千条也就几毫秒；换成"存一份标签列"才贵 —— 那份东西会漂。
    """
    cursor = _parse_at(before)
    bodies = db.scalars(select(Scratch.body)).all()
    tree = scratch_tags.count_tree(bodies)
    all_count = len(bodies)

    wanted = scratch_tags.normalize(str(tag or "").lstrip("#"))
    if wanted:
        rows = list(
            db.scalars(
                select(Scratch).order_by(Scratch.created_at.desc(), Scratch.id.desc())
            ).all()
        )
        rows = [
            one
            for one in rows
            if any(scratch_tags.under(wanted, path) for path in scratch_tags.parse(one.body))
        ]
        total = len(rows)
        if cursor is not None:
            rows = [one for one in rows if one.created_at < cursor]
        rows = rows[:limit]
    else:
        query = select(Scratch)
        if cursor is not None:
            query = query.where(Scratch.created_at < cursor)
        rows = list(
            db.scalars(
                query.order_by(Scratch.created_at.desc(), Scratch.id.desc()).limit(limit)
            ).all()
        )
        total = all_count

    return {
        "notes": [_note_json(one) for one in reversed(rows)],
        "total": int(total),
        "all": int(all_count),
        "tags": tree,
        "tag": wanted,
    }


@router.post("")
def create_note(body: dict, db: DbSession) -> dict:
    """记一条。带 `id` 时是幂等的（见文件头那段）。"""
    raw_id = str(body.get("id") or "").strip()
    if raw_id and not _ID_RE.match(raw_id):
        raise HTTPException(status_code=400, detail="id 只能是 6–32 位字母、数字、下划线或横线")
    if raw_id:
        existing = db.get(Scratch, raw_id)
        if existing is not None:
            return {"note": _note_json(existing), "created": False}

    note = Scratch(
        body=_clean_body(body.get("body")),
        **({"id": raw_id} if raw_id else {}),
    )
    db.add(note)
    try:
        db.commit()
    except IntegrityError:
        # 并发：另一个请求刚用同一个 id 落好了。回滚后读它，别报错
        #（与 `POST /plans` 的 proposalId 同一处场景）。
        db.rollback()
        existing = db.get(Scratch, raw_id)
        if existing is None:
            raise
        return {"note": _note_json(existing), "created": False}
    db.refresh(note)
    return {"note": _note_json(note), "created": True}


@router.patch("/{note_id}")
def update_note(note_id: str, body: dict, db: DbSession) -> dict:
    note = db.get(Scratch, note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="没有这条碎片")
    if "body" in body:
        note.body = _clean_body(body.get("body"))
    db.commit()
    db.refresh(note)
    return {"note": _note_json(note)}


@router.delete("/{note_id}")
def delete_note(note_id: str, db: DbSession) -> dict:
    note = db.get(Scratch, note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="没有这条碎片")
    db.delete(note)
    db.commit()
    return {"ok": True}
