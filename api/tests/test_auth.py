"""账号流程与写接口的守卫。"""

from __future__ import annotations

import uuid

PW = "correct-horse-battery"


def _email(prefix: str = "user") -> str:
    """每次生成唯一邮箱：测试共用一个库，不能用固定地址。"""
    return f"{prefix}-{uuid.uuid4().hex[:12]}@example.com"


def _register(client, email: str | None = None, password: str = PW, **extra):  # noqa: ANN001, ANN202
    return client.post(
        "/api/auth/register",
        json={"email": email or _email(), "password": password, **extra},
    )


def _csrf(client) -> dict[str, str]:  # noqa: ANN001
    return {"X-CSRF-Token": client.cookies.get("qf_csrf") or ""}


# ------------------------------------------------------------------ 注册


def test_register_creates_session_and_hides_password(client) -> None:  # noqa: ANN001
    resp = _register(client)
    assert resp.status_code == 201, resp.text

    body = resp.json()
    assert body["user"]["email"]
    assert body["user"]["displayName"]
    assert client.cookies.get("qf_session")
    # CSRF 令牌必须一起下发，否则登录后第一个写请求就会 403
    assert client.cookies.get("qf_csrf")

    # 响应里绝不能出现密码相关字段
    assert "password" not in resp.text.lower()
    assert "hash" not in resp.text.lower()


def test_email_normalized_to_lowercase(client) -> None:  # noqa: ANN001
    email = _email("MixedCase").upper()
    assert _register(client, email=email).status_code == 201

    client.cookies.clear()
    resp = client.post("/api/auth/login", json={"email": email.lower(), "password": PW})
    assert resp.status_code == 200, resp.text
    assert resp.json()["user"]["email"] == email.lower()


def test_duplicate_email_rejected(client) -> None:  # noqa: ANN001
    email = _email("dup")
    assert _register(client, email=email).status_code == 201
    client.cookies.clear()
    resp = _register(client, email=email)
    assert resp.status_code == 409


def test_password_too_short_rejected(client) -> None:  # noqa: ANN001
    resp = _register(client, password="short")
    assert resp.status_code == 422


def test_invalid_email_rejected(client) -> None:  # noqa: ANN001
    resp = _register(client, email="not-an-email")
    assert resp.status_code == 422


# ------------------------------------------------------------------ 登录


def test_login_success(client) -> None:  # noqa: ANN001
    email = _email("login")
    _register(client, email=email)
    client.cookies.clear()

    resp = client.post("/api/auth/login", json={"email": email, "password": PW})
    assert resp.status_code == 200
    assert client.cookies.get("qf_session")


def test_wrong_password_and_unknown_email_give_same_message(client) -> None:  # noqa: ANN001
    """不能通过错误文案区分「邮箱不存在」和「密码错误」，否则就是账号枚举器。"""
    known = _email("known")
    _register(client, email=known)
    client.cookies.clear()

    wrong_pw = client.post("/api/auth/login", json={"email": known, "password": "wrong-password"})
    unknown = client.post("/api/auth/login", json={"email": _email("ghost"), "password": PW})

    assert wrong_pw.status_code == unknown.status_code == 401
    assert wrong_pw.json()["detail"] == unknown.json()["detail"]


def test_login_throttle_kicks_in(client) -> None:  # noqa: ANN001
    email = _email("throttle")
    _register(client, email=email)
    client.cookies.clear()

    codes = [
        client.post("/api/auth/login", json={"email": email, "password": "nope-nope-nope"}).status_code
        for _ in range(10)
    ]
    assert 429 in codes, codes


# ------------------------------------------------------------------ 会话


def test_me_requires_login(client) -> None:  # noqa: ANN001
    client.cookies.clear()
    resp = client.get("/api/auth/me")
    assert resp.status_code == 401


def test_me_returns_current_user(client) -> None:  # noqa: ANN001
    email = _email("me")
    _register(client, email=email)
    resp = client.get("/api/auth/me")
    assert resp.status_code == 200
    assert resp.json()["user"]["email"] == email


# ------------------------------------------------------------ 写接口守卫


def test_logout_requires_csrf_token(client) -> None:  # noqa: ANN001
    """缺少双提交令牌的写请求必须被拒。"""
    _register(client)
    resp = client.post("/api/auth/logout")
    assert resp.status_code == 403

    ok = client.post("/api/auth/logout", headers=_csrf(client))
    assert ok.status_code == 200
    assert client.get("/api/auth/me").status_code == 401


def test_cross_origin_write_rejected(client) -> None:  # noqa: ANN001
    """跨站来源的写请求直接拒绝（防跨站登录）。"""
    client.cookies.clear()
    resp = client.post(
        "/api/auth/register",
        json={"email": _email("evil"), "password": PW},
        headers={"Origin": "http://evil.example.com"},
    )
    assert resp.status_code == 403


def test_same_origin_write_allowed(client) -> None:  # noqa: ANN001
    client.cookies.clear()
    resp = client.post(
        "/api/auth/register",
        json={"email": _email("same"), "password": PW},
        headers={"Origin": "http://testserver"},
    )
    assert resp.status_code == 201


# ---------------------------------------------------------------- 改密码


def test_change_password_requires_login(client) -> None:  # noqa: ANN001
    client.cookies.clear()
    resp = client.post(
        "/api/auth/password",
        json={"currentPassword": PW, "newPassword": "another-long-password"},
    )
    assert resp.status_code == 401


def test_change_password_invalidates_other_sessions(client) -> None:  # noqa: ANN001
    email = _email("rotate")
    _register(client, email=email)
    old_session = client.cookies.get("qf_session")

    resp = client.post(
        "/api/auth/password",
        json={"currentPassword": PW, "newPassword": "brand-new-password"},
        headers=_csrf(client),
    )
    assert resp.status_code == 200, resp.text

    # 改了密码，旧令牌立刻失效
    client.cookies.set("qf_session", old_session)
    assert client.get("/api/auth/me").status_code == 401

    # 新密码可用
    client.cookies.clear()
    assert client.post("/api/auth/login", json={"email": email, "password": "brand-new-password"}).status_code == 200
