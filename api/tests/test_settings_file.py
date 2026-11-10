"""设置文件的边界：只放"跨机器有意义"的设置，本机状态不落盘、不上云。

2026-11-09 之前设置文件是整个 `store.settings()` 的镜像：聊天读到哪儿
（`chatRead`，实测占文件七成）、图谱面板与布局参数（`graph`）、选题篮
（`basket`）、滤镜旧键（`skills`）都被推了进去。复制给新用户会连别人的
阅读位置一起带走，导入又会把它们清掉。

名单在 `app/settings_store.py` 的 `LOCAL_ONLY`（与 `theme/runtime/store.js`
同名常量对应）。`notes` / `places` 不在名单里 —— `marks_of()` 要从设置行里读它们
（导出分享页的批注与书签）。滤镜记录 `lens` 同样不在设置里，但它不是被"放过"：
它属于数据，住 `lens_records` 表，见 `api/tests/test_lens.py`。
"""

from __future__ import annotations

import pytest

from app import settings_file

LOCAL = ("chatRead", "graph", "basket", "skills")


def _local_payload() -> dict:
    return {
        "chatRead": {"c-1": {"mid": "m-1", "dy": 12, "bottom": False, "at": 1}},
        "graph": {"panel": {"x": 0, "y": 0, "collapsed": True}},
        "basket": ["c-0001"],
        "skills": [{"cid": "c-1", "mid": "m-1", "key": "feynman", "on": True}],
    }


def test_local_state_never_reaches_the_file(client) -> None:  # noqa: ANN001
    out = client.put(
        "/api/settings",
        json={"patch": {"theme": "light", **_local_payload()}},
    )
    assert out.status_code == 200
    assert out.json()["settings"]["theme"] == "light"
    for key in LOCAL:
        assert key not in out.json()["settings"]

    on_disk = settings_file.load()
    assert on_disk["theme"] == "light"
    for key in LOCAL:
        assert key not in on_disk


def test_only_local_state_is_rejected(client) -> None:  # noqa: ANN001
    out = client.put("/api/settings", json={"patch": _local_payload()})
    assert out.status_code == 400


def test_lens_and_skills_never_reach_the_file(client) -> None:  # noqa: ANN001
    """滤镜记录归属 `lens_records` 表（数据），设置文件不再接它们。"""
    rows = [{"cid": "c-1", "mid": "m-1", "key": "feynman", "on": True}]
    out = client.put("/api/settings", json={"patch": {"lens": rows, "skills": rows}})
    assert out.status_code == 400
    on_disk = settings_file.load()
    assert "lens" not in on_disk
    assert "skills" not in on_disk


def test_marks_still_go_to_the_file(client) -> None:  # noqa: ANN001
    """批注 / 书签不是本机状态：导出分享页要用（它们仍住设置行）。"""
    marks = {
        "notes": [{"cid": "c-1", "mid": "m-1", "text": "记住这一条"}],
        "places": [{"cid": "c-1", "mid": "m-1", "anchor": 1}],
    }
    assert client.put("/api/settings", json={"patch": marks}).status_code == 200
    on_disk = settings_file.load()
    for key, rows in marks.items():
        assert on_disk[key] == rows


def test_old_file_rows_are_filtered_on_read(client) -> None:  # noqa: ANN001
    """老文件里还躺着状态键时，读接口不能把它们暴露出去。"""
    settings_file.save({"theme": "dark", **_local_payload()})
    body = client.get("/api/settings").json()["settings"]
    assert body["theme"] == "dark"
    for key in LOCAL:
        assert key not in body


def test_sync_endpoint_filters_local_state_too(client) -> None:  # noqa: ANN001
    """`/api/progress` 是前端同步推设置的那条路，同样不能把状态键写进库。"""
    out = client.post(
        "/api/progress/sync",
        json={"settings": {"theme": "light", **_local_payload()}, "settingsRev": 1},
    )
    assert out.status_code == 200
    settings = client.get("/api/progress").json()["settings"]
    assert settings["theme"] == "light"
    for key in LOCAL:
        assert key not in settings


def test_first_save_keeps_the_example_skeleton(client) -> None:  # noqa: ANN001
    """文件还没建时第一次保存：以示例为底，别把其余设置一起抹掉。

    2026-11-09 实测：新用户只改主题，落盘的 `settings.local.json` 里只剩
    `theme` 一个键 —— 示例里填好的 AI / Obsidian / Turso 全没了。
    """
    out = client.put("/api/settings", json={"patch": {"theme": "dark"}})
    assert out.status_code == 200

    on_disk = settings_file.load()
    assert on_disk["theme"] == "dark"
    for key in ("ai", "search", "obsidian", "turso"):
        assert key in on_disk
    assert on_disk["ai"]["baseUrl"] == settings_file.example()["ai"]["baseUrl"]


def test_patch_keeps_the_rest_of_the_ai_block(client) -> None:  # noqa: ANN001
    """设置页改密钥只发 `{ai: {apiKey}}`：深合并不能把 model / baseUrl 一起吃掉。

    （2026-11-09 实测：浅合并把 `ai` 整块换成只剩 `{enabled: true}`，
    用户填完密钥聊天页仍显示"还不能对话"。）
    """
    client.put(
        "/api/settings",
        json={"patch": {"ai": {"baseUrl": "https://api.deepseek.com/v1", "model": "deepseek-chat"}}},
    )
    client.put("/api/settings", json={"patch": {"ai": {"apiKey": "sk-test"}}})

    ai = settings_file.load()["ai"]
    assert ai["apiKey"] == "sk-test"
    assert ai["baseUrl"] == "https://api.deepseek.com/v1"
    assert ai["model"] == "deepseek-chat"


def test_key_alone_is_enough_to_chat(client) -> None:  # noqa: ANN001
    """没有「启用 AI」开关：填了密钥就该是 user。"""
    client.put("/api/settings", json={"patch": {"ai": {"apiKey": "sk-test"}}})
    st = client.get("/api/ai/usage").json()
    assert st["mode"] == "user"
    assert st["hasKey"] is True


def test_import_half_json_keeps_the_skeleton(client) -> None:  # noqa: ANN001
    """剪贴板 / 别的机器拷来的半份 JSON：整份替换也要补齐骨架。"""
    out = client.put("/api/settings", json={"settings": {"theme": "dark"}, "replace": True})
    assert out.status_code == 200

    on_disk = settings_file.load()
    assert on_disk["theme"] == "dark"
    for key in ("ai", "search", "obsidian", "turso"):
        assert key in on_disk
    assert on_disk["ai"]["apiKey"] == ""


def test_old_enabled_flag_is_dropped(client) -> None:  # noqa: ANN001
    """老的「启用 AI」开关从任何入口进来都丢掉。"""
    out = client.put(
        "/api/settings",
        json={"patch": {"ai": {"enabled": True, "apiKey": "sk-test"}}},
    )
    assert "enabled" not in out.json()["settings"]["ai"]
    assert "enabled" not in settings_file.load()["ai"]


def test_turso_change_starts_sync_immediately(client, monkeypatch) -> None:  # noqa: ANN001
    """同步配置一落地就立刻跑一轮（用户 2026-11-10：复制/粘进去应该马上同步）。

    判据必须是"turso 那三项真的变了"：任何设置改动都触发的话，改个主题就会
    拉一次云端，纯属无谓。
    """
    from app import sync as sync_mod

    monkeypatch.setattr(sync_mod, "turso_enabled", lambda: True)
    monkeypatch.setattr(sync_mod, "turso_configured", lambda: True)
    started: list[int] = []
    monkeypatch.setattr(
        sync_mod, "run_in_background", lambda *a, **k: started.append(1) or True
    )

    res = client.put(
        "/api/settings",
        json={
            "patch": {
                "turso": {"url": "libsql://new.turso.io", "token": "t", "enabled": True}
            }
        },
    )

    assert res.status_code == 200, res.text
    assert res.json()["syncStarted"] is True
    assert started == [1]


def test_unrelated_setting_does_not_start_sync(client, monkeypatch) -> None:  # noqa: ANN001
    """改主题不该顺手拉一次同步。"""
    from app import sync as sync_mod

    monkeypatch.setattr(
        sync_mod, "run_in_background", lambda *a, **k: pytest.fail("不该启动同步")
    )
    res = client.put("/api/settings", json={"patch": {"theme": "dark"}})
    assert res.status_code == 200, res.text
    assert res.json()["syncStarted"] is False
