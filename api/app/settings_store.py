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


__all__ = ["load", "put", "row"]
