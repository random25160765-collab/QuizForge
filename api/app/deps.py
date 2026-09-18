"""FastAPI 依赖：**本机唯一用户**与数据库会话。

单用户本地形态（2026-09-18 起）：没有注册、没有登录、没有会话 Cookie、没有 CSRF ——
那些都是「多用户 + 浏览器」时代的产物，本地单机用不上，留着只会给"双击即用"多一道墙。

这里只保留一件事：**拿到那个本机用户**。依赖名沿用 `CurrentUser` / `AuthenticatedWriter`，
因为 9 个 router 都写着它 —— 改名没有收益，还会把 diff 摊到 40+ 处读取上。

## 为什么还留着 `users` 表与 `user_id` 列

删掉登录**不等于**推倒数据模型：单用户下 `user_id == user.id` 恒真（留着无害、也不漏数据），
而它正是将来"多设备同步"最自然的接口。这一版的目标是**去掉登录这件事**。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from .db import get_db
from .models import User

DbSession = Annotated[OrmSession, Depends(get_db)]

#: 本机用户的邮箱。刻意不用真实邮箱：将来真要接多设备同步时，别和已有的账号撞上
LOCAL_USER_EMAIL = "local@quizforge.local"
LOCAL_USER_NAME = "本机"


def get_local_user(db: OrmSession) -> User:
    """拿到本机用户；库里还没有就当场建一个。

    为什么"没有就建"而不是靠启动钩子：命令行、脚本、测试都会直接走这套依赖，
    让它们各自少一步初始化。单用户下这里不存在竞争。
    """
    user = db.scalars(select(User).order_by(User.created_at).limit(1)).first()
    if user is None:
        user = User(
            email=LOCAL_USER_EMAIL,
            # 本地形态不验口令：留空串（那一列是非空的），登录入口已经不存在了
            password_hash="",
            display_name=LOCAL_USER_NAME,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    return user


def get_current_user(request: Request, db: DbSession) -> User:
    user = get_local_user(db)
    # 挂到 request.state：日志与其它依赖能直接取用，不必再查一次
    request.state.user_id = str(user.id)
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
#: 写接口用的同一个依赖。本地不再区分"只读 / 可写"，名字留着是为了路由不必改
AuthenticatedWriter = Annotated[User, Depends(get_current_user)]
