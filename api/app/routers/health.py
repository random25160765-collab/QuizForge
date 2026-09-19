"""健康检查与运行时自述。

`/api/health` 必须不依赖鉴权 —— 部署探针与前端启动自检都要能打它。
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import __version__
from .. import ai_gateway as gateway
from .. import startup as startup_checks
from ..config import get_settings
from ..db import get_db
from ..models import BankVersion, Question, Topic
# 与 /api/bank 的 ETag 共用同一套"实时指纹"：两个地方口径不同的话，
# 前端会按其中一个判断"题库没变"，另一个改了也没用（实测就是这样卡住的）
from .bank import _current_hash as _live_hash

router = APIRouter(prefix="/api", tags=["health"])


def _build_info() -> dict:
    """读产物根下的 `build.json`（没有就如实说没有，不编一个）。"""
    path = get_settings().web_dir / "build.json"
    if not path.is_file():
        return {"stamp": "", "detail": "还没有构建信息（先 make web）"}
    try:
        return {"stamp": "", **json.loads(path.read_text(encoding="utf-8"))}
    except ValueError:
        return {"stamp": "", "detail": "build.json 读不出来"}


@router.get("/build")
def build() -> dict:
    """只报构建信息：读产物根下那份 `build.json`，**不碰数据库**。

    前端每几秒问它一次，用来判断"我手里这一页是不是旧的"（改了 `theme/` 之后
    已经打开的那一页不会自己知道）。所以它必须够轻 —— `/health` 要数题、
    要比题库指纹，不能拿来轮询。
    """
    return _build_info()


@router.get("/health")
def health(db: Session = Depends(get_db)) -> dict:
    settings = get_settings()

    database_ok = True
    detail = ""
    try:
        question_count = db.scalar(select(func.count()).select_from(Question).where(Question.retired_at.is_(None)))
        topic_count = db.scalar(select(func.count()).select_from(Topic).where(Topic.retired_at.is_(None)))
        current = db.scalar(select(BankVersion).where(BankVersion.is_current.is_(True)).order_by(BankVersion.id.desc()))
        bank = {
            "loaded": bool(question_count),
            "questions": int(question_count or 0),
            "topics": int(topic_count or 0),
            # 前端拿这个值判断"题库有没有变"——必须跟着实时数据走，
            # 否则它和 /api/bank 的 ETag 一样会永远停在导入那一刻（实测踩过：界面停在 109 道）
            "versionHash": _live_hash(db),
            "importedAt": current.imported_at.isoformat() if current else "",
        }
        bank["subjectList"] = _subject_list(db)
        bank["typeCount"] = int(
            db.scalar(
                select(func.count(func.distinct(Question.type))).where(Question.retired_at.is_(None))
            )
            or 0
        )
    except Exception as exc:  # 数据库没起来时也要能回答，否则探针只会看到连接错误
        database_ok = False
        detail = str(exc).splitlines()[0][:200]
        bank = {
            "loaded": False,
            "questions": 0,
            "topics": 0,
            "versionHash": "",
            "importedAt": "",
            "subjectList": [],
            "typeCount": 0,
        }

    return {
        "ok": database_ok,
        "version": __version__,
        # 这次运行**真正在服务的**那份前端是哪个构建（见 tools/build_web.py 的构建戳）。
        # 单文件包里它跟着包走，开发时由 `make web` 现写 —— 两边报同一个戳，
        # 才说明"给用户的和开发时看到的是同一份"（用户提过这个疑问）。
        "build": _build_info(),
        # 这条是给"我手上这个包是哪个通道"用的（内测包与正式包长得一样，
        # 只有这里能一眼看出来）。界面据此自称"内测"，用户也知道自己在试哪一版。
        "channel": settings.channel,
        "database": {"ok": database_ok, "detail": detail},
        "bank": bank,
        # AI 的接口与密钥属于**每个用户自己的设置**，服务端不持有，
        # 所以这里只说「本实例允不允许用」，不回显任何用户凭据。
        # 这个接口不需要登录，更不该暴露别人的配置。
        "ai": {
            "enabled": settings.ai_enabled,
            # 「凭据模式」没变：仍然是谁的密钥谁负责。内测通道只是让没有密钥的人
            # 也能用上本机那个模型，不改变凭据归属，所以这个字段维持 per-user。
            "mode": "per-user",
            "dailyQuota": settings.ai_daily_quota,
            # 内测通道开没开、配置里有没有密钥（**不给地址、不给密钥**：
            # 这个接口不需要登录，它只该回答"这个实例能不能免密钥用"）。
            # 注意判据是 `beta_active`：正式通道下它恒为 False —— 正式包不可能
            # 用到站长的额度，这是通道层保证的，不看配置写没写对。
            "betaEnabled": settings.beta_active,
            "betaConfigured": bool(
                str((gateway.beta_config() or {}).get("apiKey") or "").strip()
            ),
        },
    }


@router.get("/startup")
def startup() -> dict:
    """启动检查（给启动页轮询用）。

    为什么单独一条而不是塞进 `/api/health`：health 是"服务活着吗"（给监控与前端探活），
    这一条是"**能不能开始用**"（给启动页画进度条与清单）。两者的读者与节奏都不一样：
    启动页 400 毫秒问一次，health 由前端在启动时问一次。
    """
    return startup_checks.report()


def _subject_list(db: Session) -> list[dict]:
    """一级学科 + 各自题数，供首页的「覆盖范围」使用。

    覆盖范围不是用户数据（哪门学科有多少题），
    所以放在无需登录的健康检查里没有泄露问题。

    题数按**子树**汇总：题挂到知识点一级，但首页要显示的是学科总量。
    `descendants` 已经把整棵子树摊平存在一行里，所以这里不需要递归。
    两条查询搞定，不做 N+1。
    """
    subjects = db.scalars(
        select(Topic).where(Topic.retired_at.is_(None), Topic.depth == 1)
    ).all()
    counts = dict(
        db.execute(
            select(Question.topic, func.count())
            .where(Question.retired_at.is_(None))
            .group_by(Question.topic)
        ).all()
    )

    out: list[dict] = []
    for row in sorted(subjects, key=lambda item: (item.order_index, item.key)):
        # descendants 是否含自身取决于构建侧的实现，这里取并集，两种都能对上
        keys = set(row.descendants or []) | {row.key}
        total = sum(int(counts.get(key, 0) or 0) for key in keys)
        # 一道题都没有的学科不列 —— 与刷题应用里的筛选器同一条规则
        if total <= 0:
            continue
        out.append(
            {
                "key": row.key,
                "name": row.name,
                "color": row.color,
                "count": total,
                # 方向（一级分组）：首页据此算出「N 个方向」，不必再单独查一次
                "group": row.group_key,
            }
        )
    return out
