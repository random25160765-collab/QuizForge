"""同步的 HTTP 面：`GET /api/sync/status`、`POST /api/sync/run`。

跑同步要联网、首次可能几十秒，所以**不在请求里等**：`POST` 起一个后台线程，
设置页轮询 `status` 看进度。令牌只存在设置文件里，这里返回的任何字段都不含它。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .. import autosync, sync

router = APIRouter(prefix="/api/sync", tags=["sync"])


def _status() -> dict:
    out = sync.status()
    # 自动同步是后台节拍：设置页要能回答"它开着吗、多快一轮、上一轮怎样"。
    out["auto"] = autosync.status()
    return out


@router.get("/status")
def read_status() -> dict:
    return _status()


@router.post("/run")
def run_now() -> dict:
    """触发一次同步（已有一次在跑就返回 started=false）。"""
    if not sync.turso_enabled():
        raise HTTPException(status_code=400, detail="同步已关闭（设置里的 turso.enabled = false）")
    if not sync.turso_configured():
        raise HTTPException(
            status_code=400,
            detail="还没配 Turso：填数据库地址与令牌，或改 config/settings.local.json 的 turso 块",
        )
    started = sync.run_in_background()
    return {"started": started, "status": _status()}
