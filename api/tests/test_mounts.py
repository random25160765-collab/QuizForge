"""工具挂载开关：注册表分组、声明与调用两处过滤、极简模式。

用户要的是顶栏几个图标：亮起即挂载、熄灭即独立、全部熄灭即极简模式。
这里盯的是三件最容易悄悄坏掉的事：

* **每个工具都有分组**（漏一个，它就永远不会被挂载、也永远不会被关掉）；
* **`specs()` 与 `call()` 用的是同一份分组**（两处不一致的症状是"图标说关了、模型还能调"）；
* **空集是极简模式**，而"没配过"是"全都挂上"（两者混掉，升级上来的用户会发现 AI 突然变哑）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from api.app import tools  # noqa: E402
from api.app import mounts as mountlib  # noqa: E402


def test_every_tool_has_a_known_group():
    """不许有"没分组"的工具：漏一个，那个模块就再也关不掉。"""
    unknown = {name: tools.group_of(name) for name in tools.REGISTRY if tools.group_of(name) not in tools.GROUP_LABELS}
    assert unknown == {}, f"这些工具的分组不认识：{unknown}"


def test_every_group_has_at_least_one_tool():
    """也不许有"空的分组"：顶栏亮着一个关不掉的开关，比没有更糟。"""
    empty = [key for key, _, _ in tools.GROUPS if not any(tools.group_of(n) == key for n in tools.REGISTRY)]
    assert empty == [], f"这些分组一个工具都没有：{empty}"


def test_specs_follow_the_mount_set():
    everything = tools.specs(None)
    assert len(everything) == len(tools.REGISTRY), "None = 全都挂上"

    only_notes = tools.specs({"notes"})
    names = [item["function"]["name"] for item in only_notes]
    assert names, "笔记组应当有工具（search_notes / write_note）"
    assert all(tools.group_of(name) == "notes" for name in names)

    assert tools.specs(set()) == [], "空集就是极简模式：一条声明都不给"


def test_call_refuses_an_unmounted_module():
    """纵深防御：没挂载的模块，即使模型把名字报出来也不执行。"""
    ok, payload = tools.call(None, None, "run_python", {"code": "print(1)"}, {}, mounts={"notes"})
    assert ok is False
    assert "沙箱" in payload["error"], "拒的时候要说清是哪个模块没挂"

    ok, payload = tools.call(None, None, "没这个工具", {}, {}, mounts={"notes"})
    assert ok is False and "没有这个工具" in payload["error"]


def test_最小模式与没配过是两件事():
    """`read` 返回 None 表示"没配过"，`effective` 才把它翻成"全都挂上"。"""
    class _Row:
        data: dict = {}

    class _Db:
        def __init__(self, row):
            self.row = row

        def get(self, _model, _key):  # noqa: ANN001
            return self.row

    assert mountlib.read(_Db(_Row()), 1) is None
    assert mountlib.effective(_Db(_Row()), 1) == set(tools.ALL_GROUPS)

    class _Empty:
        data = {"tool_mounts": []}

    assert mountlib.read(_Db(_Empty()), 1) == set()
    assert mountlib.effective(_Db(_Empty()), 1) == set(), "明确配成空 = 极简模式，不是'全都挂上'"


def test_http_face_round_trip(tmp_path: Path, monkeypatch):
    """走一遍真接口：默认全挂 → 只挂笔记 → 极简。"""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    first = client.get("/api/chat/mounts").json()
    assert set(first["mounted"]) == set(tools.ALL_GROUPS)
    assert first["minimal"] is False
    assert len(first["declared"]) == len(tools.REGISTRY)

    only = client.post("/api/chat/mounts", json={"groups": ["notes"]}).json()
    assert only["mounted"] == ["notes"]
    assert only["declared"], "笔记组得有工具可声明"
    assert all(tools.group_of(name) == "notes" for name in only["declared"])

    minimal = client.post("/api/chat/mounts", json={"groups": []}).json()
    assert minimal["minimal"] is True
    assert minimal["declared"] == []

    bad = client.post("/api/chat/mounts", json={"groups": ["没这组"]})
    assert bad.status_code == 400, "不认识的组名要拒掉，别静默丢弃"

    # 复原（别把开发机的设置留在极简模式）
    back = client.post("/api/chat/mounts", json={"groups": list(tools.ALL_GROUPS)}).json()
    assert set(back["mounted"]) == set(tools.ALL_GROUPS)
