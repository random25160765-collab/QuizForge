"""工具挂载开关：注册表分组、声明与调用两处过滤、极简模式。

用户要的是顶栏几个图标：亮起即挂载、熄灭即独立、全部熄灭即极简模式。
这里盯的是三件最容易悄悄坏掉的事：

* **每个工具都有分组**（漏一个，它就永远不会被挂载、也永远不会被关掉）；
* **`specs()` 与 `call()` 用的是同一份分组**（两处不一致的症状是"图标说关了、模型还能调"）；
* **空集是极简模式**，而"没配过"是"全都挂上"（两者混掉，升级上来的用户会发现 AI 突然变哑）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from api.app import tools  # noqa: E402
from api.app import mounts as mountlib  # noqa: E402


def test_every_tool_has_a_known_group():
    """不许有"没分组"的工具：漏一个，那个模块就再也关不掉。

    **元能力除外**（`tools.META_TOOLS`，现在只有 `run_subagent`）：它刻意不属于任何
    一块 —— 它是"把长工具链派出去"的通用手段，挂了**任何**工具时都该在
    （见 `tools.specs` 的放行规则）。它也不能反过来被塞进某个组，那会让
    "关掉资料组"顺手把派活的能力也关掉。
    """
    unknown = {
        name: tools.group_of(name)
        for name in tools.REGISTRY
        if name not in tools.META_TOOLS and tools.group_of(name) not in tools.GROUP_LABELS
    }
    assert unknown == {}, f"这些工具的分组不认识：{unknown}"
    assert all(tools.group_of(name) == "" for name in tools.META_TOOLS), "元能力不该被塞进某个组"


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
    # 元能力不在任何组里，但它要跟着走：挂了任何工具就有它
    assert "run_subagent" in names
    assert all(tools.group_of(name) == "notes" for name in names if name not in tools.META_TOOLS)

    assert tools.specs(set()) == [], "空集就是极简模式：一条声明都不给（元能力也不例外）"


def test_call_refuses_an_unmounted_module():
    """纵深防御：没挂载的模块，即使模型把名字报出来也不执行。"""
    ok, payload = tools.call(None, None, "run_python", {"code": "print(1)"}, {}, mounts={"notes"})
    assert ok is False
    assert "沙箱" in payload["error"], "拒的时候要说清是哪个模块没挂"

    ok, payload = tools.call(None, None, "没这个工具", {}, {}, mounts={"notes"})
    assert ok is False and "没有这个工具" in payload["error"]


def test_最小模式与没配过是两件事(db_session):
    """`read` 返回 None 表示"没配过"，`effective` 才把它翻成"全都挂上"。

    这里原来用的是一个手写的假 db（只实现了 `get(model, key)`）—— 那是按
    `user_id` 取设置时代的形状。单用户下取设置不该经过"哪个用户"，所以改成
    走真夹具（`settings_store` 会把本机用户与那一行都准备好）。
    """
    assert mountlib.read(db_session) is None
    assert mountlib.effective(db_session) == set(tools.ALL_GROUPS)

    mountlib.write(db_session, [])
    assert mountlib.read(db_session) == set()
    assert mountlib.effective(db_session) == set(), "明确配成空 = 极简模式，不是'全都挂上'"


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
    assert all(tools.group_of(name) == "notes" for name in only["declared"] if name not in tools.META_TOOLS)
    assert "run_subagent" in only["declared"], "元能力跟着任何一组走（它不属于任何一块）"

    minimal = client.post("/api/chat/mounts", json={"groups": []}).json()
    assert minimal["minimal"] is True
    assert minimal["declared"] == []

    bad = client.post("/api/chat/mounts", json={"groups": ["没这组"]})
    assert bad.status_code == 400, "不认识的组名要拒掉，别静默丢弃"

    # 复原（别把开发机的设置留在极简模式）
    back = client.post("/api/chat/mounts", json={"groups": list(tools.ALL_GROUPS)}).json()
    assert set(back["mounted"]) == set(tools.ALL_GROUPS)


# ---------------------------------------------------------------- 模式与权限档


def test_modes_are_group_times_access():
    """三个模式：极简空、查询只读、学习全放。"""
    from app import mounts as m
    from app import tools

    assert m.MODE_KEYS == ("minimal", "query", "study")
    one = {item.key: item for item in m.MODES}
    assert one["minimal"].groups == () and one["minimal"].access == ("read",)
    assert one["query"].groups == ("notes", "library", "graph")
    assert one["query"].access == ("read",)
    assert set(one["study"].access) == set(tools.ACCESS_LEVELS)


def test_colors_have_one_source(db_session):
    """三个颜色只有**一处**来源（用户："注意不要硬编码颜色 —— 未来如果我想改颜色呢？
    那应该是一键式的"）。

    默认在 `Mode.color` 里；设置里的 `mode_colors` 可以整体覆盖。
    CSS 里一个色号都不写，只认前端注入的 `--mode-*`。
    """
    from app import mounts as m

    palette = m.colors()
    assert set(palette) == set(m.MODE_KEYS)
    assert all(value.startswith("#") and len(value) == 7 for value in palette.values())

    # 走一遍设置覆盖：改成什么就是什么，不用碰代码
    from app import settings_store

    settings_store.put(db_session, **{m.COLORS_KEY: {"study": "#FF0000"}})
    assert m.colors(db_session)["study"] == "#FF0000"
    assert m.colors(db_session)["query"] == palette["query"], "只覆盖点名的那个"
    # 接口那份也要跟着变（前端就是靠它注入 CSS 变量的）
    described = m.describe(db_session)
    assert described["colors"]["study"] == "#FF0000"
    assert [x for x in described["modes"] if x["key"] == "study"][0]["color"] == "#FF0000"
    settings_store.put(db_session, **{m.COLORS_KEY: None})


def test_access_filters_tools():
    """同一组里，只读档筛掉写与执行。"""
    from app import tools

    read_only = [s["function"]["name"] for s in tools.specs(set(tools.ALL_GROUPS), ("read",))]
    assert "get_mastery" in read_only            # 只读的留下
    assert "write_note" not in read_only          # 直接写的筛掉
    assert "grade_problem" not in read_only
    assert "run_python" not in read_only          # 执行的筛掉
    assert "mark_mastered" not in read_only       # 只提案的也筛掉

    full = [s["function"]["name"] for s in tools.specs(set(tools.ALL_GROUPS), tools.ACCESS_LEVELS)]
    assert len(full) == len(tools.REGISTRY)


def test_call_refuses_off_level(db_session, local_user):
    """报出名字也不执行：越档调用被拒，且给的是可读的错。"""
    from app import tools

    ok, payload = tools.call(
        db_session, local_user, "write_note", {"note": "x", "text": "y"},
        {}, mounts=set(tools.ALL_GROUPS), allow=("read",),
    )
    assert ok is False
    assert "没有开" in payload["error"]


# ------------------------------------------------- 输入锚定模式（用户定的规则）

def test_snapshot_maps_back_to_the_mode():
    """`from_snapshot`：从那条输入当时记下的组名，还原出"哪几组、放到哪一档"。

    规则："一条输入可以有多个输出，输入锚定模式，因此所有的输出都是一个模式，
    要想改模式，必须更改输入。" —— 生成时读的就是这份快照，
    **不是**"现在设置里是什么"。所以这几条断了，坏的是"同一句话两个输出不同模式"。
    """
    from app import mounts as m
    from app import tools

    groups, allow = m.from_snapshot('["notes", "library", "graph"]')
    assert m.mode_of_groups(groups) == "query"
    assert allow == ("read",)

    # 用 `tools.ALL_GROUPS` 而不是把那一串组名抄进来：**加一组就要来改一次测试**，
    # 而这条测试要验的是"快照能还原成模式"，不是"学习模式恰好有这五组"
    #（原先抄的是手写的五组，加 `web` 那天它就红了 —— 红得没有信息量）。
    groups, allow = m.from_snapshot(json.dumps(list(tools.ALL_GROUPS)))
    assert m.mode_of_groups(groups) == "study"
    assert set(allow) == set(tools.ACCESS_LEVELS)

    # 老消息的快照（加组之前那份）对不上任何预设 → 按它**自己记的那几组**走。
    # 这是对的：那一轮确实没有新组，不该被追认。
    groups, allow = m.from_snapshot('["notes", "library", "graph", "quiz", "sandbox"]')
    assert m.mode_of_groups(groups) == "custom"
    assert len(groups) == 5 and "web" not in groups

    # 空数组 = 极简（**不是**"没记过"）：没挂任何组也是一个明确的模式
    assert m.from_snapshot("[]") == (set(), ("read",))

    # 没记过 / 坏数据 → None（调用方回落到当前设置）
    assert m.from_snapshot("") is None
    assert m.from_snapshot(None) is None
    assert m.from_snapshot("{ 不是 json") is None


def test_snapshot_keeps_custom_combinations():
    """手动组合（对不上任何预设）：照样按它自己的组来，档位全放。"""
    from app import mounts as m
    from app import tools

    groups, allow = m.from_snapshot('["notes"]')
    assert groups == {"notes"}
    assert m.mode_of_groups(groups) == "custom"
    assert set(allow) == set(tools.ACCESS_LEVELS)
