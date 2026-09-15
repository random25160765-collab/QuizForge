"""进度的读接口、鉴权与账号隔离。

跨设备的合并语义（累加、幂等、乱序、补丁不得写计数）在
`test_cross_device.py` 里，本文件只覆盖访问控制与账号边界。
"""

from __future__ import annotations

import uuid

from app.deps import CSRF_COOKIE

PASSWORD = "password-1234"


def _register(client, email: str) -> dict:  # noqa: ANN001
    client.cookies.clear()
    resp = client.post("/api/auth/register", json={"email": email, "password": PASSWORD})
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


def _headers(client) -> dict:  # noqa: ANN001
    return {"X-CSRF-Token": client.cookies.get(CSRF_COOKIE) or ""}


def _attempt(question_id: str, *, at: int, status: str = "correct") -> dict:
    return {
        "id": str(uuid.uuid4()),
        "questionId": question_id,
        "at": at,
        "day": "2026-09-15",
        "status": status,
        "score": 1.0,
        "topicKey": "c",
    }


# ---------------------------------------------------------------- 方法语义


def test_progress_read_does_not_require_csrf(client) -> None:  # noqa: ANN001
    """读接口不该要求 CSRF 令牌。

    这是踩过的坑：`GET` 误用了写接口的依赖（`AuthenticatedWriter` 内含
    CSRF 双提交校验），浏览器端直接 403 —— 因为 GET 本来就不带那个头。
    """
    _register(client, f"read-{uuid.uuid4().hex[:8]}@example.com")
    assert client.get("/api/progress").status_code == 200


def test_progress_write_requires_csrf(client) -> None:  # noqa: ANN001
    _register(client, f"write-{uuid.uuid4().hex[:8]}@example.com")
    assert client.post("/api/progress/sync", json={"attempts": []}).status_code == 403


def test_progress_requires_login(client) -> None:  # noqa: ANN001
    client.cookies.clear()
    assert client.get("/api/progress").status_code == 401
    assert client.post("/api/progress/sync", json={"attempts": []}).status_code == 401
    assert client.post("/api/progress/reset").status_code == 401


def test_snapshot_shape(client) -> None:  # noqa: ANN001
    """快照的字段是前端 `hydrate` 直接消费的，形状不能变。"""
    _register(client, f"shape-{uuid.uuid4().hex[:8]}@example.com")
    body = client.get("/api/progress").json()

    assert set(body) == {"records", "days", "settings", "settingsRev", "rev"}
    assert body["records"] == {}
    assert body["days"] == {}


# ------------------------------------------------------------------ 设置


def test_settings_are_stored_and_returned(client) -> None:  # noqa: ANN001
    _register(client, f"set-{uuid.uuid4().hex[:8]}@example.com")

    client.post(
        "/api/progress/sync",
        json={"settings": {"theme": "light", "basket": ["c-0001"]}, "settingsRev": 1},
        headers=_headers(client),
    )
    settings = client.get("/api/progress").json()["settings"]
    assert settings["theme"] == "light"
    assert settings["basket"] == ["c-0001"]


def test_stale_settings_rev_is_ignored(client) -> None:  # noqa: ANN001
    """rev 不更大的设置写入被忽略 —— 客户端要靠 Lamport 抬高自己的 rev。"""
    _register(client, f"setrev-{uuid.uuid4().hex[:8]}@example.com")
    sync = "/api/progress/sync"

    client.post(sync, json={"settings": {"theme": "light"}, "settingsRev": 5}, headers=_headers(client))
    client.post(sync, json={"settings": {"theme": "dark"}, "settingsRev": 2}, headers=_headers(client))

    assert client.get("/api/progress").json()["settings"]["theme"] == "light"


def test_reset_clears_progress_but_keeps_settings(client) -> None:  # noqa: ANN001
    """重置清空学习数据，但保留设置 —— 设置是偏好，不是进度。"""
    _register(client, f"reset-{uuid.uuid4().hex[:8]}@example.com")
    sync = "/api/progress/sync"

    client.post(sync, json={"attempts": [_attempt("c-0001", at=1789459200000)]}, headers=_headers(client))
    client.post(sync, json={"settings": {"theme": "light"}, "settingsRev": 1}, headers=_headers(client))

    assert client.post("/api/progress/reset", headers=_headers(client)).status_code == 200

    snapshot = client.get("/api/progress").json()
    assert snapshot["records"] == {}
    assert snapshot["days"] == {}
    assert snapshot["settings"]["theme"] == "light"


# ------------------------------------------------------------------ 隔离


def test_progress_is_isolated_between_users(client) -> None:  # noqa: ANN001
    """用户 A 的进度不能出现在用户 B 的快照里。"""
    a = _register(client, f"a-{uuid.uuid4().hex[:8]}@example.com")
    client.post(
        "/api/progress/sync",
        json={"attempts": [_attempt("c-0001", at=1789459200000)]},
        headers=_headers(client),
    )
    assert client.get("/api/progress").json()["records"]["c-0001"]["attempts"] == 1

    b = _register(client, f"b-{uuid.uuid4().hex[:8]}@example.com")
    assert b["user"]["id"] != a["user"]["id"]
    assert client.get("/api/progress").json()["records"] == {}, "B 不该看到 A 的进度"


def test_two_users_same_question_do_not_collide(client) -> None:  # noqa: ANN001
    """不同用户作答同一道题互不影响（记录主键是 (user_id, question_id)）。"""
    _register(client, f"w1-{uuid.uuid4().hex[:8]}@example.com")
    client.post(
        "/api/progress/sync",
        json={"attempts": [_attempt("c-0001", at=1789459200000)]},
        headers=_headers(client),
    )

    _register(client, f"w2-{uuid.uuid4().hex[:8]}@example.com")
    result = client.post(
        "/api/progress/sync",
        json={"attempts": [_attempt("c-0001", at=1789459200000)]},
        headers=_headers(client),
    ).json()

    assert result["attemptsAccepted"] == 1, "另一个用户的同名题目应当被正常接受"
    assert client.get("/api/progress").json()["records"]["c-0001"]["attempts"] == 1
