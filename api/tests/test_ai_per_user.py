"""AI 批改：**每个用户用自己填的密钥**。

服务端不持有共享密钥，所以「没填密钥」是一等公民的正常状态，
必须给出可操作的提示（去设置里填），而不是含糊的「服务端未配置」。
这里把这个契约钉住。
"""

from __future__ import annotations

import uuid

PASSWORD = "password-1234"


def _register(client) -> str:  # noqa: ANN001
    """本机用户就绪（单用户本地形态没有"注册"这回事），返回它的 id。

    名字与签名留着，是为了不动几十个调用点 —— 见 `app/deps.py`。
    """
    from conftest import local_user_id

    return local_user_id()


def _headers(client) -> dict:  # noqa: ANN001
    # CSRF 随账号面一起删掉了；这个函数留着同样是为了不动调用点
    return {}


def _set_ai(client, **conf) -> None:  # noqa: ANN001
    """把 AI 配置写进该用户的设置里（与设置面板走同一条通道）。"""
    resp = client.post(
        "/api/progress/sync",
        json={"settings": {"ai": conf}, "settingsRev": 1},
        headers=_headers(client),
    )
    assert resp.status_code == 200, resp.text


def _grade(client) -> object:  # noqa: ANN001
    return client.post(
        "/api/ai/grade",
        json={"messages": [{"role": "user", "content": "hi"}]},
        headers=_headers(client),
    )


# ------------------------------------------------------------------ 没配


def test_grade_without_key_points_at_settings(client) -> None:  # noqa: ANN001
    """没填密钥时，提示必须说清「去哪儿填」。

    「服务端未配置」这种话会让用户以为要找站长，
    而实际上按钮就在他自己的设置面板里。
    """
    _register(client)
    resp = _grade(client)
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert "设置" in detail
    assert "密钥" in detail


def test_grade_when_disabled_says_so(client) -> None:  # noqa: ANN001
    _register(client)
    _set_ai(client, enabled=False, apiKey="sk-user-own-key", baseUrl="http://127.0.0.1:9/v1")
    resp = _grade(client)
    assert resp.status_code == 503
    assert "启用" in resp.json()["detail"]


def test_enabled_but_empty_key_is_rejected(client) -> None:  # noqa: ANN001
    """开了开关但密钥是空的 —— 仍然要拦住，且提示指向密钥。"""
    _register(client)
    _set_ai(client, enabled=True, apiKey="   ", baseUrl="http://127.0.0.1:9/v1")
    resp = _grade(client)
    assert resp.status_code == 503
    assert "密钥" in resp.json()["detail"]


# ------------------------------------------------------------------ 配了


def test_grade_uses_the_users_own_endpoint(client) -> None:  # noqa: ANN001
    """配好之后，服务端拿**用户填的地址**去调。

    这里指向一个必然连不上的本地端口：512/502 都说明「它确实去调了用户那个地址」，
    而不是被某种共享默认值拦下或改道。密钥是假的，绝不会真的打到供应商。
    """
    _register(client)
    _set_ai(client, enabled=True, apiKey="sk-user-own-key", baseUrl="http://127.0.0.1:9/v1")
    resp = _grade(client)
    assert resp.status_code in (502, 504), resp.text
    assert "你填写的接口" in resp.json()["detail"]


def test_ping_without_key_guides_instead_of_failing(client) -> None:  # noqa: ANN001
    """连通性测试是设置面板上的按钮，没填密钥时它应当**照常返回 200**，
    在结果里说明该做什么 —— 返回 4xx 会让面板显示成一个报错弹窗。"""
    _register(client)
    body = client.get("/api/ai/ping").json()
    assert body["ok"] is False
    assert body["perUser"] is True
    assert "密钥" in body["message"]


def test_usage_tells_whether_the_user_has_a_key(client) -> None:  # noqa: ANN001
    """设置面板靠 hasKey 决定要不要提示「你还没填密钥」。"""
    _register(client)
    assert client.get("/api/ai/usage").json()["hasKey"] is False

    _set_ai(client, enabled=True, apiKey="sk-user-own-key", baseUrl="http://127.0.0.1:9/v1")
    body = client.get("/api/ai/usage").json()
    assert body["hasKey"] is True
    assert body["enabled"] is True
    assert body["today"] == 0


# ------------------------------------------------------------------ 不泄露


def test_health_does_not_expose_any_credentials(client) -> None:  # noqa: ANN001
    """健康检查不需要登录，所以更不能暴露任何用户凭据。"""
    body = client.get("/api/health").json()
    assert body["ai"]["mode"] == "per-user"
    assert "apiKey" not in body["ai"]
    assert "baseUrl" not in body["ai"]
