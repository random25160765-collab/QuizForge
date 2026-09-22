"""FastAPI 依赖：数据库会话。

## 这里曾经有"本机唯一用户"

`get_local_user` / `get_current_user` / `CurrentUser` / `AuthenticatedWriter`
都在 2026-09-22 删掉了 —— 连同 `users` 表与 10 张表的 `user_id` 外键
（见 `models.py` 顶部那段）。

它们是两步走的产物：2026-09-18 只删掉了**登录面**（注册 / 登录 / 会话 / CSRF），
但留了一个"取本机唯一用户、没有就当场建"的依赖，理由是"将来多设备同步的接口"。
结果是 `user` 这个参数被一路透传进 8 个 router 和 40+ 个函数签名，
而它携带的信息只有 `user.id` —— 一个恒定的 UUID，用来填那些外键。

单机应用里没有"用户"这个词的指代：**没有第二个人，也就没有归属要记**。
所以这一层现在只剩数据库会话。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session as OrmSession

from .db import get_db

DbSession = Annotated[OrmSession, Depends(get_db)]

__all__ = ["DbSession"]
