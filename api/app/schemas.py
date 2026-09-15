"""出入参模型。

字段一律用 camelCase 对外（前端是手写 JS，历史上所有 JSON 都是 camelCase）。
内部保持 Python 的 snake_case，靠别名生成器转换。
"""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator
from pydantic.alias_generators import to_camel

# 密码下限取 8：再短在字典攻击面前没有意义；上限防超长输入拖垮 argon2
PASSWORD_MIN = 8
PASSWORD_MAX = 128

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ApiModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
        str_strip_whitespace=True,
    )


# ------------------------------------------------------------------ 账号


class RegisterIn(ApiModel):
    email: EmailStr
    password: str = Field(min_length=PASSWORD_MIN, max_length=PASSWORD_MAX)
    display_name: str = Field(default="", max_length=64)


class LoginIn(ApiModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=PASSWORD_MAX)


class ChangePasswordIn(ApiModel):
    current_password: str = Field(max_length=PASSWORD_MAX)
    new_password: str = Field(min_length=PASSWORD_MIN, max_length=PASSWORD_MAX)


class UserOut(ApiModel):
    id: str
    email: str
    display_name: str
    created_at: datetime


class MeOut(ApiModel):
    user: UserOut
    # 会话过期时间，前端可据此在到期前提示重新登录
    expires_at: datetime | None = None


class OkOut(ApiModel):
    ok: bool = True


# ------------------------------------------------------------------ 工具


def normalize_email(raw: str) -> str:
    """邮箱统一小写去空白，避免出现大小写不同的重复账号。"""
    return (raw or "").strip().lower()


def is_valid_email(raw: str) -> bool:
    return bool(_EMAIL_RE.match(normalize_email(raw)))


__all__ = [
    "PASSWORD_MAX",
    "PASSWORD_MIN",
    "ApiModel",
    "ChangePasswordIn",
    "LoginIn",
    "MeOut",
    "OkOut",
    "RegisterIn",
    "UserOut",
    "is_valid_email",
    "normalize_email",
]
