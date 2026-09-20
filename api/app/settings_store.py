"""这台机器的设置：单用户形态下唯一的那一行 `user_settings`。

**为什么还要一层**：表按 `user_id` 分行是登录时代的形状 —— `deps.py` 的注释
交代了为什么不动 `user_id`（单用户下 `user_id == user.id` 恒真，删它是一次全库
重构）。但"取设置"这件事在**业务代码**里不该再看见"用户"：谁在调、是哪个用户，
这一层自己解决。

这样还顺手挡住一类错误：调用点把 `user_id` 传错（比如传了别的 id），静默读到
另一行 —— 单用户下读到的会是空设置，看起来像"我的开关自己重置了"。
"""

from __future__ import annotations

from typing import Any

from .deps import get_local_user
from .models import UserSettings


def row(db: Any, *, create: bool = False) -> Any:
    """那唯一一行。`create=True` 时不存在就建出来。"""
    user = get_local_user(db)
    found = db.get(UserSettings, user.id)
    if found is None and create:
        found = UserSettings(user_id=user.id, data={})
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
