"""进度的读接口与设置语义。

跨设备的合并语义（累加、幂等、乱序、补丁不得写计数）在 `test_cross_device.py` 里，
本文件只覆盖快照形状、设置与重置。

（原先还有"鉴权与账号隔离"那一节 —— 单用户本地形态下没有账号，整节删掉了，
见 `app/deps.py`。）
"""

from __future__ import annotations

import uuid

PASSWORD = "password-1234"


def _register(client, *args, **kwargs) -> str:  # noqa: ANN001
    """不再需要做什么 —— 账号系统已整体拆除（2026-09-22，见 `app/models.py` 顶部）。

    保留这个空函数只是为了不动几十个调用点：它在用例里当"开工准备"用，
    而现在没有任何准备工作要做（数据隔离由 conftest 的 autouse fixture 负责）。
    """
    return ""


def _headers(client) -> dict:  # noqa: ANN001
    # CSRF 随账号面一起删掉了；留着是为了不动调用点
    return {}


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


# ------------------------------------------------------------------ 无凭据


# 这里原先有三条"访问控制"用例：读接口不该要 CSRF、写接口必须带 CSRF、没登录一律 401。
# 单用户本地形态下这三件事一起消失了（没有 CSRF、没有登录）—— 见 `app/deps.py`。
# 取而代之的护栏在 `test_route_contract.py`：那些依赖不许挂回来。


def test_progress_reads_without_any_credential(client) -> None:  # noqa: ANN001
    """本地单用户：不带任何凭据，读进度就该拿到 200。"""
    assert client.get("/api/progress").status_code == 200


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


# 这里原先还有一节「隔离」：两个账号的进度互不可见、答同一道题记录不撞。
# 单用户本地形态下没有第二个账号（见 `app/deps.py`），这一节随之删掉 ——
# 记录主键里那个 user_id 仍在，只是恒为同一个值。
