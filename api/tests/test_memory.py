"""全局记忆：跨对话长期信息的读写、注入与边界。

用户要的是「AI 跨对话记得住」——所以这里盯三件事：

* 设置页的增删改查**真的落库**（不是浏览器本地的一份缓存）；
* 记忆**不默认填进提示词**（用户 2026-11-09 定的："放在那边，让 agent 有需要
  的时候看"，像 coding agent 的 global memory）—— 它走 `list_memory` 工具，
  提示词里只留使用指引；极简模式没有工具，也就没有记忆；
* 它属于用户数据：换台机器要还在（`BOUNDARY` 的 user、进同步行）。
"""

from __future__ import annotations

from app import datapack, memory, sync, tools
from app.routers.chat import GROUP_PROMPTS, build_prompt


def test_memory_crud_over_http(client):  # noqa: ANN001
    created = client.post(
        "/api/memory",
        json={"title": "叫我 rd", "body": "回复用中文，别叫老师", "kind": "preference"},
    )
    assert created.status_code == 200, created.text
    row = created.json()["memory"]
    assert row["kind"] == "preference"
    assert row["kindLabel"] == "偏好 / 要求"
    assert row["createdAt"] and row["updatedAt"]

    listed = client.get("/api/memory").json()["memories"]
    assert [one["title"] for one in listed] == ["叫我 rd"]

    patched = client.patch(
        f"/api/memory/{row['id']}",
        json={"body": "回复用中文；称呼 rd", "kind": "profile"},
    )
    assert patched.status_code == 200
    assert patched.json()["memory"]["body"] == "回复用中文；称呼 rd"
    assert patched.json()["memory"]["kind"] == "profile"

    assert client.delete(f"/api/memory/{row['id']}").json()["ok"] is True
    assert client.get("/api/memory").json()["memories"] == []


def test_empty_memory_is_refused(client):  # noqa: ANN001
    response = client.post("/api/memory", json={"title": "  ", "body": ""})
    assert response.status_code == 400
    assert "至少要有一个" in response.json()["detail"]


def test_title_falls_back_to_the_first_line(client):  # noqa: ANN001
    """模型偶尔只给正文：标题从第一行取一句，而不是存一条没有名字的记忆。"""
    row = client.post("/api/memory", json={"body": "在做 QFStudio\n同步是重点"}).json()[
        "memory"
    ]
    assert row["title"] == "在做 QFStudio"


def test_memory_tools_create_list_update_delete(db_session):  # noqa: ANN001
    ok, created = tools.call(
        db_session,
        "edit_memory",
        {
            "action": "create",
            "title": "睡眠不好",
            "body": "入睡困难，晚上别排深度任务",
            "kind": "profile",
        },
    )
    assert ok, created
    assert created["mode"] == "created"
    memory_id = created["memory"]["id"]

    # 同标题再记一次 = 更新，不是攒第二条同义的
    ok, again = tools.call(
        db_session,
        "edit_memory",
        {"action": "create", "title": "睡眠不好", "body": "凌晨一点后才睡"},
    )
    assert ok and again["mode"] == "updated"
    assert again["memory"]["id"] == memory_id
    assert again["memory"]["body"] == "凌晨一点后才睡"

    ok, listed = tools.call(db_session, "list_memory", {})
    assert ok and listed["count"] == 1
    assert listed["memories"][0]["id"] == memory_id

    # 按标题定位（模型不一定记得回查 id）
    ok, updated = tools.call(
        db_session,
        "edit_memory",
        {"action": "update", "title": "睡眠不好", "kind": "fact"},
    )
    assert ok and updated["memory"]["kind"] == "fact"

    ok, deleted = tools.call(db_session, "edit_memory", {"action": "delete", "id": memory_id})
    assert ok and deleted["deleted"] == "睡眠不好"
    assert tools.call(db_session, "list_memory", {})[1]["count"] == 0


def test_memory_is_not_injected_into_the_prompt(db_session):  # noqa: ANN001
    """用户定的：记忆**不默认填进提示词**，放在那边让 agent 有需要时看。

    所以就算库里记得满满的，提示词里也不该出现记忆内容 —— 只留使用指引
    （memory 组那段：有这么个东西、什么时候该查）。极简模式没有工具，
    也就完全看不到记忆。
    """
    memory.create(
        db_session,
        title="叫他 rd",
        body="回复用中文；他在准备数字芯片设计的面试",
        kind="profile",
    )

    full = build_prompt(set(GROUP_PROMPTS))
    assert "叫他 rd" not in full, "记忆不该被默认塞进提示词"
    assert "回复用中文" not in full
    # 指引还在：它得知道有这么个东西、什么时候去查
    assert "list_memory" in full

    minimal = build_prompt(set())
    assert "叫他 rd" not in minimal
    assert "list_memory" not in minimal, "极简模式没有工具，也就没有记忆"


def test_memories_are_user_data():  # noqa: ANN001
    """换台机器打开，记忆得还在：归 user、进用户包、进行级同步。

    2026-11-09 用户要求：记忆是**关于他**的长期信息，和对话正文一样
    要端到端加密上云 —— 解密用的口令只在自己机器上。
    """
    assert datapack.BOUNDARY["memories"] == "user"
    assert "memories" in datapack.USER_TABLES
    assert "memories" in sync.SYNC_TABLES
    assert "memories" in sync.PRIVATE_TABLES, "记忆要加密，不再明文上云"


def test_memory_group_is_read_only_in_query_mode():  # noqa: ANN001
    """查询模式能看全（`list_memory`），改记忆要写档 —— 那留给学习模式。"""
    from app import mounts

    query = next(one for one in mounts.MODES if one.key == "query")
    assert "memory" in query.groups
    names = {
        item["function"]["name"] for item in tools.specs(set(query.groups), query.access)
    }
    assert "list_memory" in names
    assert "edit_memory" not in names
