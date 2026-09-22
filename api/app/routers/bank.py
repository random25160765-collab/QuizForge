"""题库读取接口。

返回体就是 `tools/dataset.build_dataset` 的产物，前端 `data.install()` 直接消费，
索引、主题树、筛选逻辑一行都不用改。

为什么仍是一次返回全量：前端的筛选（主题树 / 题型 / 难度 / 掌握度 / 关键词）
全部在本地内存里跑，改成服务端筛选意味着把整套筛选逻辑搬到后端，
与「保留现有运行时」的既定方向相反。代价是首屏要下载整份题库，
用 ETag 协商缓存兜住 —— 内容没变时只有一次 304，不重传正文。

题量真的涨到下载成为瓶颈时，扩展点是本接口加 `?topics=` 之类的分片参数，
而不是把筛选挪到服务端。
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select

from ..bank_import import current_bank
from ..deps import DbSession
from ..models import BankVersion

router = APIRouter(prefix="/api", tags=["bank"])


def _current_hash(db) -> str:  # noqa: ANN001
    """题库指纹**必须来自实时数据**，不能来自导入快照。

    实测踩过：`BankVersion.content_hash` 是"导入那一刻"的旧哈希 ——
    导完之后流水线又发布了 500 多道题，这个哈希一直没变 ✗，
    于是客户端带 `If-None-Match` 来问、服务端一直回 **304** ✗，
    用户界面（工作台与选题）永远停在旧的 109 道 ✔。

    现在按"在库、未退役题目的数量 + 最近更新时间 + 最大 id"算指纹：
    每次发布都会变，且不必扫全表正文。
    """
    import hashlib  # noqa: PLC0415

    from sqlalchemy import text  # noqa: PLC0415

    row = db.execute(
        text(
            "SELECT COUNT(*), MAX(updated_at), MAX(id) FROM questions WHERE retired_at IS NULL"
        )
    ).one()
    return hashlib.sha256(f"{row[0]}|{row[1]}|{row[2]}".encode("utf-8")).hexdigest()


@router.get("/bank")
def get_bank(request: Request, db: DbSession) -> Response:
    """返回整份题库。

    需要登录：题目是本产品的核心内容，不该让未登录的请求拉走全量。
    首页那几个规模数字走 `/api/health`（不需要登录，且只暴露数量）。
    """
    etag = f'"{_current_hash(db)}"'

    if request.headers.get("if-none-match") == etag:
        # 内容没变：回 304，浏览器直接用本地缓存
        return Response(
            status_code=304,
            headers={"ETag": etag, "Cache-Control": "private, no-cache"},
        )

    payload = current_bank(db)
    return JSONResponse(
        payload,
        headers={
            "ETag": etag,
            # private：带用户凭证，不能让中间代理缓存后发给别人
            # no-cache：允许存，但每次必须带 ETag 回来校验
            "Cache-Control": "private, no-cache",
        },
    )


@router.get("/bank/version")
def bank_version(db: DbSession) -> dict:
    """轻量版本探测：题库更新后前端可以据此提示刷新。"""
    version = db.scalars(
        select(BankVersion).where(BankVersion.is_current.is_(True)).order_by(BankVersion.id.desc())
    ).first()
    if version is None:
        return {"hash": "empty", "questions": 0, "topics": 0, "importedAt": ""}
    return {
        "hash": version.content_hash,
        "questions": version.question_count,
        "topics": version.topic_count,
        "importedAt": version.imported_at.isoformat(timespec="seconds"),
    }
