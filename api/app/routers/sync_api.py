"""同步的 HTTP 面：`GET /api/sync/status`、`POST /api/sync/run`。

跑同步要联网、首次可能几十秒，所以**不在请求里等**：`POST` 起一个后台线程，
设置页轮询 `status` 看进度。常规状态响应不含令牌与口令；唯一的例外是
`/plugin-config` —— 设置页要把三样配置复制给 Obsidian 插件，按需请求一次。
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


@router.get("/plugin-config")
def read_plugin_config() -> dict:
    """Obsidian 云同步插件要填的三样，一次给全（设置页「复制」按需请求）。

    与 `turso.token` 同权限：能打开本机设置页的人本来就能看到令牌。口令来自
    `config/settings.local.json` 的 `syncPassphrase`（或环境变量），也不**写进任何日志**；
    这个响应只在用户按下复制时被请求一次。
    """
    conf = sync.turso_config()
    return {
        "url": str(conf.get("url") or "").strip(),
        "token": str(conf.get("token") or "").strip(),
        "passphrase": sync.passphrase(),
    }


@router.post("/passphrase")
def write_passphrase(body: dict) -> dict:
    """存下同步口令，并立刻催一轮。

    手机端没有文件管理器，口令只能从这个入口填 —— 写进 `config/settings.local.json`
    的 `syncPassphrase`（`FILE_ONLY`：不落库、不同步、不回显，响应里只有"有没有"）。
    """
    try:
        out = sync.save_passphrase(str(body.get("passphrase") or ""))
    except sync.SyncError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    started = False
    if sync.turso_enabled() and sync.turso_configured():
        started = sync.run_in_background()
    return {"ok": True, **out, "started": started, "status": _status()}


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
