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
        return {"file": str(settings_file.path()), "settings": data}
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

    if replace:
        data = dict(payload)
    else:
        data = settings_file.merge(payload)

    target = settings_file.save(data)
    # 镜像到数据库：别的读 `settings_store.row()` 的老路径照样能看到（row 本身以文件为准）
    settings_store.put(db, **data)
    return {"ok": True, "file": str(target), "settings": data}
