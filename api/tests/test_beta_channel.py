"""内测通道：没有自带密钥的人也能聊，靠实例级那份配置（`config/ai.local.json`）。

四条契约：

* 优先级是 **用户密钥 > 内测通道 > 明确报错**（不能静默降级成"用别人的额度"）
* 密钥只在**文件**里，不进 Settings（那个对象会被 repr、被日志打印）
* 文件按 mtime 懒读：改完立刻生效，不必重启
* 连不上时说清该找谁 —— 走内测通道的人该去找站长，而不是去翻自己的密钥

注意：`app.config.get_settings` 带 `lru_cache`，改环境变量已经晚了，
所以这里替换的是模块里的 `get_settings`；通道配置则写进一个临时文件。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from app import ai_gateway
from app.config import get_settings
from app.routers import ai as ai_router
from app.routers import health as health_router

PASSWORD = "password-1234"
DEAD = "http://127.0.0.1:9/v1"  # 必然连不上


def _register(client, *args, **kwargs) -> str:  # noqa: ANN001
    """不再需要做什么 —— 账号系统已整体拆除（2026-09-22，见 `app/models.py` 顶部）。

    保留这个空函数只是为了不动几十个调用点：它在用例里当"开工准备"用，
    而现在没有任何准备工作要做（数据隔离由 conftest 的 autouse fixture 负责）。
    """
    return ""


def _headers(client) -> dict:  # noqa: ANN001
    # CSRF 随账号面一起删掉了；留着是为了不动调用点
    return {}


def _set_ai(client, **conf) -> None:  # noqa: ANN001
    resp = client.post(
        "/api/progress/sync",
        json={"settings": {"ai": conf}, "settingsRev": 1},
        headers=_headers(client),
    )
    assert resp.status_code == 200, resp.text


def _channel(monkeypatch, tmp_path: Path, **beta):  # noqa: ANN001
    """把内测通道指向一份临时配置（默认是个连不上的地址）。"""
    payload = {
        "enabled": True,
        "baseUrl": DEAD,
        "model": "beta-test-model",
        "apiKey": "sk-beta-test",
        **beta,
    }
    path = tmp_path / "ai.local.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    conf = get_settings().model_copy(
        update={"ai_beta_enabled": True, "ai_beta_config_file": path}
    )
    monkeypatch.setattr(ai_gateway, "get_settings", lambda: conf)
    monkeypatch.setattr(ai_router, "get_settings", lambda: conf)
    monkeypatch.setattr(health_router, "get_settings", lambda: conf)
    ai_gateway._BETA_CACHE.update({"path": "", "mtime": 0.0, "data": {}})
    return path


def _no_channel(monkeypatch) -> None:  # noqa: ANN001
    conf = get_settings().model_copy(update={"ai_beta_enabled": False})
    monkeypatch.setattr(ai_gateway, "get_settings", lambda: conf)
    monkeypatch.setattr(ai_router, "get_settings", lambda: conf)
    monkeypatch.setattr(health_router, "get_settings", lambda: conf)


def _grade(client) -> object:  # noqa: ANN001
    return client.post(
        "/api/ai/grade",
        json={"messages": [{"role": "user", "content": "hi"}]},
        headers=_headers(client),
    )


# ------------------------------------------------------------------ 优先级


def test_no_key_falls_back_to_the_beta_channel(client, monkeypatch, tmp_path) -> None:
    """没填密钥不再直接 503 —— 退到内测通道，真去调那份配置里的地址。

    连不上是预期内的（测试里给的是死地址），关键是**报错来自通道**，
    说明它确实走了这条路，而不是把用户拦在门外。
    """
    _channel(monkeypatch, tmp_path)
    _register(client)

    resp = _grade(client)
    assert resp.status_code in (502, 504), resp.text
    detail = resp.json()["detail"]
    assert "内测通道" in detail and "站长" in detail, detail


def test_users_own_key_wins_over_the_channel(client, monkeypatch, tmp_path) -> None:  # noqa: ANN001
    """填了自己密钥的人走自己的 —— 内测通道是给没有密钥的人的。"""
    _channel(monkeypatch, tmp_path)
    _register(client)
    _set_ai(client, enabled=True, apiKey="sk-mine", baseUrl=DEAD, model="my-model")

    detail = _grade(client).json()["detail"]
    assert "你填写的接口" in detail, detail
    assert "内测通道" not in detail, "不该被内测通道顶掉"


def test_user_switching_own_key_off_falls_back_to_the_channel(client, monkeypatch, tmp_path) -> None:  # noqa: ANN001
    """用户把自己那个开关关掉 = "别用我的密钥"，而不是"我不想用 AI"。"""
    _channel(monkeypatch, tmp_path)
    _register(client)
    _set_ai(client, enabled=False, apiKey="sk-mine", baseUrl=DEAD)

    assert "内测通道" in _grade(client).json()["detail"]


def test_no_key_and_no_channel_is_a_clear_503(client, monkeypatch) -> None:  # noqa: ANN001
    _no_channel(monkeypatch)
    _register(client)

    resp = _grade(client)
    assert resp.status_code == 503
    assert "设置" in resp.json()["detail"] and "密钥" in resp.json()["detail"]


def test_channel_without_a_key_says_where_to_fix_it(client, monkeypatch, tmp_path) -> None:  # noqa: ANN001
    """文件在、但 apiKey 是空的：这是**配置写错了**，得指向那份文件。"""
    _channel(monkeypatch, tmp_path, apiKey="")
    _register(client)

    resp = _grade(client)
    assert resp.status_code == 503
    assert "ai.local.json" in resp.json()["detail"]


def test_broken_file_is_treated_as_no_channel(client, monkeypatch, tmp_path) -> None:  # noqa: ANN001
    """坏文件当成"没有" —— 一个手写的 JSON 不该让整站 AI 全挂。"""
    path = tmp_path / "ai.local.json"
    path.write_text("{ 这不是 json", encoding="utf-8")
    conf = get_settings().model_copy(
        update={"ai_beta_enabled": True, "ai_beta_config_file": path}
    )
    monkeypatch.setattr(ai_gateway, "get_settings", lambda: conf)
    monkeypatch.setattr(ai_router, "get_settings", lambda: conf)
    ai_gateway._BETA_CACHE.update({"path": "", "mtime": 0.0, "data": {}})
    _register(client)

    assert _grade(client).status_code == 503


def test_editing_the_file_takes_effect_without_a_restart(client, monkeypatch, tmp_path) -> None:  # noqa: ANN001
    """按 mtime 懒读：改完立刻生效（否则每次改配置都要重启服务，实测很难受）。"""
    path = _channel(monkeypatch, tmp_path)
    _register(client)
    assert client.get("/api/ai/usage").json()["betaModel"] == "beta-test-model"

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["model"] = "beta-改过的模型"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    assert client.get("/api/ai/usage").json()["betaModel"] == "beta-改过的模型"


# ------------------------------------------------------------------ 对话


def test_chat_stream_reports_the_channel_when_it_is_down(client, monkeypatch, tmp_path) -> None:  # noqa: ANN001
    """对话页看到的那句话必须能指导下一步（去找站长 / 自己填密钥）。"""
    _channel(monkeypatch, tmp_path)
    _register(client)
    cid = client.post(
        "/api/chat/conversations", json={}, headers=_headers(client)
    ).json()["conversation"]["id"]

    body = client.post(
        f"/api/chat/conversations/{cid}/messages",
        json={"content": "在吗"},
        headers=_headers(client),
    ).text
    events = {}
    for block in body.strip().split("\n\n"):
        name, data = "", {}
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if name:
            events[name] = data

    assert "error" in events, body
    assert "内测通道" in events["error"]["message"]
    assert events["error"]["retryable"] is True, "上游连不上是暂时问题，值得重试"


# ------------------------------------------------------------------ 面板可见性


def test_usage_says_which_channel_is_in_use(client, monkeypatch, tmp_path) -> None:  # noqa: ANN001
    _channel(monkeypatch, tmp_path)
    _register(client)

    body = client.get("/api/ai/usage").json()
    assert body["mode"] == "beta" and body["enabled"] is True
    assert body["betaModel"] == "beta-test-model"
    assert body["label"] and body["hasKey"] is False

    _set_ai(client, enabled=True, apiKey="sk-mine", baseUrl=DEAD, model="my-model")
    body = client.get("/api/ai/usage").json()
    assert body["mode"] == "user" and body["hasKey"] is True


def test_usage_says_none_when_the_channel_is_off(client, monkeypatch) -> None:  # noqa: ANN001
    _no_channel(monkeypatch)
    _register(client)
    body = client.get("/api/ai/usage").json()
    assert body["mode"] == "none" and body["enabled"] is False


def test_health_shows_the_switch_but_never_the_secret(client, monkeypatch, tmp_path) -> None:  # noqa: ANN001
    """健康检查不需要登录，所以只回答"能不能免密钥用"，不给地址也不给密钥。"""
    _channel(monkeypatch, tmp_path)
    body = client.get("/api/health").json()["ai"]
    assert body["betaEnabled"] is True and body["betaConfigured"] is True
    assert "baseUrl" not in body and "apiKey" not in body and "betaModel" not in body
    assert body["mode"] == "per-user", "凭据模式没变：内测通道不改变密钥归属"

    raw = client.get("/api/health").text
    assert "sk-beta-test" not in raw, "密钥一个字都不能出现在这个接口里"
