"""账号：注册 / 登录 / 登出 / 当前用户 / 改密码。

安全取舍：

- 登录失败一律回同一句话，不区分「邮箱不存在」与「密码错误」——
  否则这个接口就成了账号枚举器。
- 登录失败按 (IP, 邮箱) 计数限流。单进程内存计数，部署多进程时
  需要挪到 Redis；这点写在注释里，免得日后误以为已经全局生效。
- 注册成功后直接建会话：注册完还要再登一次是多余的一步。
- 写接口分两层守卫：``guard_write`` 校验来源（防跨站登录），
  已登录的写接口再加 ``require_csrf``（双提交令牌）。
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as OrmSession

from ..config import get_settings
from ..db import get_db
from ..deps import (
    AuthenticatedWriter,
    clear_session_cookies,
    get_optional_user,
    guard_write,
    set_csrf_cookie,
    set_session_cookie,
)
from ..models import User
from ..schemas import ChangePasswordIn, LoginIn, MeOut, OkOut, RegisterIn, UserOut, normalize_email
from ..security import (
    create_session,
    destroy_all_sessions,
    destroy_session,
    hash_password,
    needs_rehash,
    verify_password,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])

WriteGuard = Depends(guard_write)

# ---------------------------------------------------------------- 限流
# 单进程内存计数。多进程 / 多副本部署时每个进程各算一份，实际阈值会被放大，
# 那种场景要换成 Redis 计数器。这里的目标只是拦住单机上的暴力破解。
_FAILED: dict[str, list[float]] = {}
_MAX_FAILURES = 8
_FAILURE_WINDOW = 300.0  # 秒


def _client_ip(request: Request) -> str:
    # 反向代理后面拿到的是代理 IP，需要按部署实际情况读 X-Forwarded-For
    return (request.client.host if request.client else "") or "-"


def _throttle_key(request: Request, email: str) -> str:
    return f"{_client_ip(request)}|{email}"


def _check_throttle(key: str) -> None:
    now = time.monotonic()
    hits = [t for t in _FAILED.get(key, []) if now - t < _FAILURE_WINDOW]
    if hits:
        _FAILED[key] = hits
    else:
        _FAILED.pop(key, None)
    if len(hits) >= _MAX_FAILURES:
        retry = int(_FAILURE_WINDOW - (now - hits[0]))
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"尝试过于频繁，请 {max(retry, 1)} 秒后再试",
        )


def _record_failure(key: str) -> None:
    _FAILED.setdefault(key, []).append(time.monotonic())


def _clear_failures(key: str) -> None:
    _FAILED.pop(key, None)


# ------------------------------------------------------------------ 工具


def _to_out(user: User) -> UserOut:
    return UserOut(
        id=str(user.id),
        email=user.email,
        display_name=user.display_name or user.email.split("@")[0],
        created_at=user.created_at,
    )


def _start_session(request: Request, response: Response, db: OrmSession, user: User) -> datetime:
    token, row = create_session(
        db,
        user,
        user_agent=request.headers.get("user-agent", ""),
        ip=_client_ip(request),
    )
    db.commit()
    set_session_cookie(response, token)
    # 会话与 CSRF 令牌一起下发，前端登录后立刻能做写操作
    set_csrf_cookie(response)
    return row.expires_at


# ------------------------------------------------------------------ 注册


@router.post(
    "/register",
    response_model=MeOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[WriteGuard],
)
def register(
    payload: RegisterIn,
    request: Request,
    response: Response,
    db: Annotated[OrmSession, Depends(get_db)],
) -> MeOut:
    email = normalize_email(payload.email)
    if db.scalar(select(User.id).where(User.email == email)) is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该邮箱已注册")

    user = User(
        email=email,
        password_hash=hash_password(payload.password),
        display_name=(payload.display_name or email.split("@")[0])[:64],
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        # 并发注册同一邮箱时由唯一索引兜底
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该邮箱已注册") from None

    expires_at = _start_session(request, response, db, user)
    return MeOut(user=_to_out(user), expires_at=expires_at)


# ------------------------------------------------------------------ 登录


@router.post("/login", response_model=MeOut, dependencies=[WriteGuard])
def login(
    payload: LoginIn,
    request: Request,
    response: Response,
    db: Annotated[OrmSession, Depends(get_db)],
) -> MeOut:
    email = normalize_email(payload.email)
    key = _throttle_key(request, email)
    _check_throttle(key)

    user = db.scalar(select(User).where(User.email == email))
    if user is None or user.disabled or not verify_password(user.password_hash, payload.password):
        _record_failure(key)
        # 不区分「邮箱不存在」与「密码错误」
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="邮箱或密码不正确")

    # argon2 参数升级后在登录时顺手重算，用户无感
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(payload.password)
    user.last_login_at = datetime.now(UTC)

    _clear_failures(key)
    expires_at = _start_session(request, response, db, user)
    return MeOut(user=_to_out(user), expires_at=expires_at)


# ------------------------------------------------------------------ 登出


@router.post("/logout", response_model=OkOut, dependencies=[WriteGuard])
def logout(
    request: Request,
    response: Response,
    user: AuthenticatedWriter,
    db: Annotated[OrmSession, Depends(get_db)],
) -> OkOut:
    token = request.cookies.get(get_settings().cookie_name, "")
    if token:
        destroy_session(db, token)
        db.commit()
    clear_session_cookies(response)
    return OkOut()


# -------------------------------------------------------------- 当前用户


@router.get("/me", response_model=MeOut)
def me(request: Request, response: Response, db: Annotated[OrmSession, Depends(get_db)]) -> MeOut:
    """未登录返回 401。

    前端把它当作启动守卫：401 → 跳登录页。这里顺带补发 CSRF Cookie，
    保证「刷新页面后立刻能写」不会因为缺令牌而 403。
    """
    user = get_optional_user(request, db)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录")

    if not request.cookies.get("qf_csrf"):
        set_csrf_cookie(response)

    expires_at = getattr(request.state, "session_expires_at", None)
    return MeOut(user=_to_out(user), expires_at=expires_at)


# ---------------------------------------------------------------- 改密码


@router.post("/password", response_model=OkOut, dependencies=[WriteGuard])
def change_password(
    payload: ChangePasswordIn,
    request: Request,
    response: Response,
    user: AuthenticatedWriter,
    db: Annotated[OrmSession, Depends(get_db)],
) -> OkOut:
    if not verify_password(user.password_hash, payload.current_password):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="当前密码不正确")

    user.password_hash = hash_password(payload.new_password)
    db.flush()
    # 改密码后踢掉所有会话（含其它浏览器），再给当前这个重新发一个
    destroy_all_sessions(db, user.id)
    _start_session(request, response, db, user)
    return OkOut()


__all__ = ["router"]
