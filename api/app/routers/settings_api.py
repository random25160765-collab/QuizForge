"""整份设置的读写：`GET/PUT /api/settings`。

文件是权威（`config/settings.local.json`，见 `app/settings_file.py`）：

* `GET` 给设置页一份完整、可拷贝的 JSON；
* `PUT` 写回文件（同时镜像到数据库那一行，给不读文件的旧路径兜底）。

这样三条路就通了：

1. 界面上改 → `PUT` → 文件；
2. CLI/脚本改文件 → 设置页打开时 `GET` → 界面上看得见；
3. 新用户复制一份 `config/settings.example.json` 改名成 `settings.local.json` 即可。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .. import settings_file, settings_store
from ..deps import DbSession

router = APIRouter(prefix="/api/settings", tags=["settings"])

#: 整份设置的上限：防止有人把一篇笔记贴进来
MAX_BYTES = 512 * 1024


@router.get("")
def read(db: DbSession) -> dict:
    data = settings_file.load()
    if data:
        # 老文件里可能还有本机状态键（读到哪儿 / 面板位置），读出去之前滤掉
        return {"file": str(settings_file.path()), "settings": settings_store.normalize(data)}
    # 文件还没建（第一次启动）：给数据库那份，前端照常能编辑
    return {"file": "", "settings": settings_store.load(db)}


@router.put("")
def write(body: dict, db: DbSession) -> dict:
    """默认是**合并一个 patch**；要整份替换得显式带 `replace: true`。

    为什么默认合并：设置有好几个页面在用（对话页切主题、设置页改 AI……），
    每个页面手里都是一份**可能过期的**整份设置。让它们整份 PUT 会把别的页
    刚写进去的键冲掉 —— 实测把 `obsidian.vaultPath` 冲成了空串。
    所以：谁改了哪个键就只提交哪个键；只有"导入设置"这种明确要覆盖的场景才整份替换。
    """
    replace = bool(body.get("replace"))
    payload = body.get("settings") if isinstance(body.get("settings"), dict) else body.get("patch")
    if not isinstance(payload, dict) or not payload:
        raise HTTPException(status_code=400, detail="空的设置")
    if len(str(payload)) > MAX_BYTES:
        raise HTTPException(status_code=413, detail="设置太大了（上限 512KB）")
    if not isinstance(payload.get("ai"), (dict, type(None))):
        raise HTTPException(status_code=400, detail="ai 必须是对象")

    # 本机状态键一概不落文件（前端已过滤；这里是第二道，curl / 旧客户端也挡得住）
    payload = settings_store.normalize(payload)
    if not payload:
        raise HTTPException(status_code=400, detail="里面只有本机状态，没有设置")

    if replace:
        # 整份替换也过骨架：手写的、从剪贴板粘的、别的机器拷来的 JSON 都可能只有半份，
        # 落盘的那份必须是完整可编辑的设置（缺的键走默认，而不是消失）。
        data = settings_file.complete(payload)
    else:
        data = settings_file.merge(payload)
    # 合并路会把旧文件里的状态键带出来，一起剔掉 —— 下一次写入就顺手把文件洗干净
    data = settings_store.normalize(data)

    before = settings_file.load()
    target = settings_file.save(data)
    # 镜像到数据库：别的读 `settings_store.row()` 的老路径照样能看到（row 本身以文件为准）。
    # 传 None = 删键：库里那份以前存过本机状态，这里一并清掉。
    settings_store.put(db, **data, **{key: None for key in settings_store.LOCAL_ONLY})

    # 同步配置一落地就立刻跑一轮（用户 2026-11-10：复制/粘进去应该马上开始同步，
    # 不该等他再去点一次「立即同步」）。判据是**真的变了**：改主题之类不该触发。
    old_turso = before.get("turso") if isinstance(before.get("turso"), dict) else {}
    new_turso = data.get("turso") if isinstance(data.get("turso"), dict) else {}
    turso_changed = any(
        str(old_turso.get(key) or "") != str(new_turso.get(key) or "")
        for key in ("url", "token", "enabled")
    )
    started = False
    if turso_changed:
        from .. import sync as sync_mod  # noqa: PLC0415 —— 别让设置读写拖上加密依赖

        if sync_mod.turso_enabled() and sync_mod.turso_configured():
            started = sync_mod.run_in_background()
    return {"ok": True, "file": str(target), "settings": data, "syncStarted": started}
