"""密码、会话令牌与 CSRF。

## 为什么是服务端会话而不是 JWT

数据隔离要求每个查询都强制带 ``user_id``。服务端会话能一键失效
（登出全部设备、改密码、封禁账号），而 JWT 在过期前无法收回；
前端也不需要有 token 存储、续期、并发刷新那一套。
代价是每个请求多一次会话表查询 —— 一次主键/唯一索引命中，可以接受。

## 令牌怎么存

Cookie 里放明文随机串，库里只存它的 sha256。即使数据库被读走，
里面的哈希也无法直接用来冒充登录。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from .config import get_settings
from .models import Session, User

_hasher = PasswordHasher()

# ---------------------------------------------------------------- 密码


def hash_password(raw: str) -> str:
    return _hasher.hash(raw)


def verify_password(stored_hash: str, raw: str) -> bool:
    """校验密码。任何异常都当作「不匹配」，不要把差异暴露给调用方。"""
    try:
        _hasher.verify(stored_hash, raw)
        return True
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    """参数升级后（argon2 默认值随版本变化）在下次登录时无感重算。"""
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return True


# ---------------------------------------------------------------- 令牌


def new_session_token() -> str:
    return secrets.token_urlsafe(get_settings().session_token_bytes)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def constant_time_eq(left: str, right: str) -> bool:
    return hmac.compare_digest(left, right)


# ---------------------------------------------------------------- 会话


def create_session(db: OrmSession, user: User, *, user_agent: str = "", ip: str = "") -> tuple[str, Session]:
    """签发一个新会话，返回 (明文令牌, 会话行)。明文只在这一刻存在。"""
    settings = get_settings()
    token = new_session_token()
    now = datetime.now(UTC)
    row = Session(
        user_id=user.id,
        token_hash=token_digest(token),
        expires_at=now + timedelta(days=settings.session_ttl_days),
        last_seen_at=now,
        user_agent=user_agent[:255],
        ip=ip[:64],
    )
    db.add(row)
    db.flush()
    return token, row


def resolve_session(db: OrmSession, token: str) -> Session | None:
    """按令牌取会话；过期则就地删除并返回 None。"""
    if not token:
        return None
    row = db.scalar(select(Session).where(Session.token_hash == token_digest(token)))
    if row is None:
        return None

    now = datetime.now(UTC)
    expires_at = row.expires_at
    # SQLite 等方言可能回读成 naive datetime，统一按 UTC 处理
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= now:
        db.delete(row)
        db.flush()
        return None

    # 节流写 last_seen_at：每次请求都写会白白放大写放大
    last_seen = row.last_seen_at
    if last_seen is not None and last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=UTC)
    if last_seen is None or (now - last_seen) > timedelta(minutes=5):
        row.last_seen_at = now
        db.flush()
    return row


def destroy_session(db: OrmSession, token: str) -> None:
    row = db.scalar(select(Session).where(Session.token_hash == token_digest(token)))
    if row is not None:
        db.delete(row)
        db.flush()


def destroy_all_sessions(db: OrmSession, user_id) -> int:  # noqa: ANN001
    rows = db.scalars(select(Session).where(Session.user_id == user_id)).all()
    for row in rows:
        db.delete(row)
    db.flush()
    return len(rows)


def purge_expired_sessions(db: OrmSession) -> int:
    rows = db.scalars(select(Session).where(Session.expires_at <= datetime.now(UTC))).all()
    for row in rows:
        db.delete(row)
    if rows:
        db.flush()
    return len(rows)


# ----------------------------------------------------------------- CSRF


def new_csrf_token() -> str:
    return secrets.token_urlsafe(24)
