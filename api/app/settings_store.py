"""这台机器的设置：`app_settings` 表里唯一的那一行。

**为什么还要一层**：设置整份存一个 JSON（`AppSettings.data`），
读写的调用点很多（挂载开关、主题、选题篮……），让它们各写一次
`db.get(AppSettings, 1)` 既啰嗦、又容易忘 `create=True`（于是读到一个 None，
表现是"我的开关自己重置了"）。这一层自己解决"那一行在不在、要不要建"。

（2026-09-22 之前这里是 `user_settings` 表 + `get_local_user`：
取的是"本机唯一用户"名下那一行。用户没了，那层间接也没了。）
"""

from __future__ import annotations

from typing import Any

from .models import AppSettings

#: 单例那一行固定用它 —— 表本来就是个单行表，主键只是个占位。
SETTINGS_ID = 1


def row(db: Any, *, create: bool = False) -> Any:
    """那唯一一行。`create=True` 时不存在就建出来。"""
    found = db.get(AppSettings, SETTINGS_ID)
    if found is None and create:
        found = AppSettings(id=SETTINGS_ID, data={})
        db.add(found)
        db.commit()
    return found


def load(db: Any) -> dict:
    """整份设置（读不到就是空 dict）。"""
    found = row(db)
    data = getattr(found, "data", None)
    return dict(data) if isinstance(data, dict) else {}


def marks_of(conf: dict, cid: Any) -> dict:
    """某条会话里的**痕迹**：`{"notes": [...], "places": [...]}`（可含老的 `stations`）。

    痕迹就是用户自己划下的那些笔（前端的 `QF.store`，住在设置的 `notes` / `places`
    两个键里，见 `theme/runtime/store.js` 的 `addMark` / `addPlace`）：
    `notes` = 高亮 / 删除线 / 下划线 / **批注**，`places` = **书签** / 回溯。

    为什么要有这一层：痕迹与设置同住一行 JSON，但**每条自己带 `cid`** —— 取的时候
    必须按会话过滤，否则导出的分享页会把**别的对话**的圈点也画上来（渲染只按 `mid`
    找，见 `store.js` 的 `marksOf`；`mid` 是消息 id，跨会话不会撞，但把别条对话的
    记号塞进一个"发给别人"的文件里仍然是不对的）。

    **口径只此一处**：导出网页（`routers/chat.py`）与"他自己留下过什么记号"
    （`recursion.py`）都从这里取，谁也别自己写一遍 filter。
    """
    key = str(cid)
    data = conf if isinstance(conf, dict) else {}
    out: dict = {}
    for name in ("notes", "places", "stations"):
        rows = data.get(name)
        if not isinstance(rows, list):
            continue
        hit = [one for one in rows if isinstance(one, dict) and str(one.get("cid") or "") == key]
        if hit:
            out[name] = hit
    return out


def put(db: Any, **changes: Any) -> dict:
    """改几个键。`None` = 删掉这个键（"没配过"与"配成空"是两件事，见 mounts）。"""
    found = row(db, create=True)
    data = dict(getattr(found, "data", None) or {})
    for key, value in changes.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    found.data = data
    db.commit()
    return data


__all__ = ["load", "marks_of", "put", "row"]
