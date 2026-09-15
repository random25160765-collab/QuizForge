"""FastAPI 依赖：当前用户、CSRF、Cookie 读写。

**所有业务查询都必须经由这里拿到用户 id**，不要从请求体或查询参数里读 user_id ——
那等于把隔离交给客户端自觉。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session as OrmSession

from .config import get_settings
from .db import get_db
from .models import User
from .security import constant_time_eq, new_csrf_token, resolve_session

CSRF_COOKIE = "qf_csrf"
CSRF_HEADER = "X-CSRF-Token"
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

DbSession = Annotated[OrmSession, Depends(get_db)]


class NotAuthenticated(HTTPException):
    def __init__(self) -> None:
        super().__init__(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录")


class CsrfFailed(HTTPException):
    def __init__(self, detail: str = "CSRF 校验失败") -> None:
        super().__init__(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


# ---------------------------------------------------------------- 用户


def get_optional_user(request: Request, db: DbSession) -> User | None:
    settings = get_settings()
    token = request.cookies.get(settings.cookie_name, "")
    row = resolve_session(db, token)
    if row is None:
        return None
    user = db.get(User, row.user_id)
    if user is None or user.disabled:
        return None
    # 挂到 request.state，日志与后续依赖都能取用而无需再查一次
    request.state.user_id = str(user.id)
    return user


def get_current_user(user: Annotated[User | None, Depends(get_optional_user)]) -> User:
    if user is None:
        raise NotAuthenticated()
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
OptionalUser = Annotated[User | None, Depends(get_optional_user)]


# ------------------------------------------------------------- 请求校验


def verify_same_origin(request: Request) -> None:
    """校验 Origin / Referer 与本站一致。

    对登录、注册这类**尚未持有 CSRF 令牌**的写请求，这是主要防线
    （防御「把受害者登进攻击者账号」这类跨站登录）。
    没有 Origin 与 Referer 时放行：命令行 curl、服务端调用都属于这种，
    它们本来就不携带浏览器 Cookie。
    """
    source = request.headers.get("origin") or request.headers.get("referer") or ""
    if not source:
        return

    settings = get_settings()
    allowed = set(settings.cors_origin_list)
    host = request.headers.get("host", "")
    for scheme in ("http", "https"):
        allowed.add(f"{scheme}://{host}")

    origin = source.split("/", 3)
    normalized = f"{origin[0]}//{origin[2]}" if len(origin) >= 3 else source
    if normalized not in allowed:
        raise CsrfFailed(f"跨站请求被拒绝（来源 {normalized}）")


def require_csrf(request: Request) -> None:
    """双提交令牌：Cookie 里的值必须与请求头一致。

    必须在 ``verify_same_origin`` 之上叠加，而不是二选一 ——
    前者防跨站、后者防同站子域或 XSS 借道（Cookie 与头都由脚本控制时仍会一致，
    所以它防的是「攻击者无法读取 Cookie」这一前提被破坏得更早的情形）。
    """
    sent = request.headers.get(CSRF_HEADER, "")
    stored = request.cookies.get(CSRF_COOKIE, "")
    if not sent or not stored or not constant_time_eq(sent, stored):
        raise CsrfFailed()


def get_authenticated_writer(user: CurrentUser, _csrf: Annotated[None, Depends(require_csrf)]) -> User:
    """已登录写接口的统一依赖。

    刻意把「鉴权」和「CSRF」串在**同一个依赖里**，而不是用
    ``dependencies=[CsrfGuard]``：路由级依赖会先于参数依赖执行，
    匿名请求会拿到 403（CSRF 失败）而不是 401（未登录），
    既误导前端，也等于告诉了探测者服务端的状态。
    同一条依赖内按参数声明顺序解析，鉴权必然先跑。
    """
    return user


AuthenticatedWriter = Annotated[User, Depends(get_authenticated_writer)]


def guard_write(request: Request) -> None:
    """所有写请求的统一入口依赖。"""
    if request.method in UNSAFE_METHODS:
        verify_same_origin(request)


# ---------------------------------------------------------------- Cookie


def set_session_cookie(response: Response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        settings.cookie_name,
        token,
        max_age=settings.session_ttl_days * 86400,
        httponly=True,                 # JS 读不到会话令牌
        samesite="lax",                # 跨站表单提交不携带，兼顾体验与安全
        secure=settings.cookie_secure,
        path="/",
    )


def set_csrf_cookie(response: Response, token: str | None = None) -> str:
    """下发 CSRF 令牌。

    刻意**不加 HttpOnly** —— 前端要读出来放进请求头，这正是双提交成立的前提。
    """
    settings = get_settings()
    value = token or new_csrf_token()
    response.set_cookie(
        CSRF_COOKIE,
        value,
        max_age=settings.session_ttl_days * 86400,
        httponly=False,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )
    return value


def clear_session_cookies(response: Response) -> None:
    settings = get_settings()
    response.delete_cookie(settings.cookie_name, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
