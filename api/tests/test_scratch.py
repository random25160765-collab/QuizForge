"""碎片（Scratchpad）：随手记一句的地方。

它刻意**不是**待办也不是笔记（见 `models.Scratch`）：没有完成态、没有分类、
没有标题，只有一句话和它是什么时候写的。这里盯的就是这条定位最容易塌的几处：

* 读出来是**旧 → 新**（这一栏是聊天记录的排法，最新的一条在底下）；
* 同一个 `id` 只落一行 —— 发送失败重试、两个标签页各点一次，都只该有一条；
* 空的一条拒绝（一次误触的回车不该在记录里留下一个空气泡）；
* 它是**用户数据**：`BOUNDARY` 归 user、同步清单里有它、正文加密上传 ——
  少任何一条，"换台机器还记得"就会在这个功能上不成立，而其它页都是好的，
  很难发现。
"""

from __future__ import annotations

from app import datapack, scratch_tags, sync, tools


def _post(client, body: str, note_id: str = "") -> dict:  # noqa: ANN001
    payload: dict = {"body": body}
    if note_id:
        payload["id"] = note_id
    response = client.post("/api/scratch", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def test_notes_come_back_oldest_first(client):  # noqa: ANN001
    """三句话依次记下：读回来必须还是这个顺序（界面上最新的一条在底下）。"""
    for text in ("先记一句", "再补一句", "最后一句"):
        assert _post(client, text)["created"] is True

    listed = client.get("/api/scratch").json()
    assert [one["body"] for one in listed["notes"]] == ["先记一句", "再补一句", "最后一句"]
    assert listed["total"] == 3


def test_the_same_id_does_not_make_two_notes(client):  # noqa: ANN001
    """同一条重试两次：第二次返回既有那行（`created: false`），库里仍只有一条。"""
    first = _post(client, "网页版写完就断网了", note_id="0123456789abcdef0123456789abcdef")
    assert first["created"] is True

    again = _post(client, "网页版写完就断网了", note_id="0123456789abcdef0123456789abcdef")
    assert again["created"] is False
    assert again["note"]["id"] == first["note"]["id"]

    listed = client.get("/api/scratch").json()
    assert listed["total"] == 1
    assert len(listed["notes"]) == 1


def test_a_blank_note_is_refused(client):  # noqa: ANN001
    response = client.post("/api/scratch", json={"body": "   \n  "})
    assert response.status_code == 400
    assert "空的一条不留" in response.json()["detail"]


def test_editing_and_deleting(client):  # noqa: ANN001
    note = _post(client, "先这么写")["note"]

    changed = client.patch(f"/api/scratch/{note['id']}", json={"body": "改一下措辞"})
    assert changed.status_code == 200, changed.text
    assert changed.json()["note"]["body"] == "改一下措辞"
    assert changed.json()["note"]["createdAt"] == note["createdAt"], "改字不该把时间也改掉"

    assert client.delete(f"/api/scratch/{note['id']}").status_code == 200
    assert client.get("/api/scratch").json()["total"] == 0
    # 删过之后再删 / 再改：404，而不是静默成功
    assert client.delete(f"/api/scratch/{note['id']}").status_code == 404
    assert client.patch(f"/api/scratch/{note['id']}", json={"body": "还在吗"}).status_code == 404


def test_an_id_with_weird_characters_is_refused(client):  # noqa: ANN001
    """id 会当主键进同步日志：带斜杠 / 空格的一律不收，给一句能读懂的话。"""
    response = client.post("/api/scratch", json={"id": "a/b c", "body": "带怪 id 的一条"})
    assert response.status_code == 400
    assert "id" in response.json()["detail"]


def test_tags_live_in_the_text_and_build_a_tree(client):  # noqa: ANN001
    """标签不存第二份：写 `#a/b` 就是标签，读出来带树与条数（父含子）。"""
    _post(client, "#Books/札记/grammar 逗号的用法")
    _post(client, "#Books/札记/《论语》 另一句")
    _post(client, "没有标签的一句")

    data = client.get("/api/scratch").json()
    assert data["all"] == 3
    assert data["notes"][0]["tags"] == ["Books/札记/grammar"]
    assert data["notes"][2]["tags"] == []

    top = {one["path"]: one for one in data["tags"]}
    assert top["Books"]["count"] == 2
    assert top["Books"]["children"][0]["path"] == "Books/札记"
    assert top["Books"]["children"][0]["count"] == 2


def test_a_parent_tag_includes_its_children(client):  # noqa: ANN001
    """点父标签要看得见子标签的碎片 —— 但"全部"的条数不跟着变小。"""
    _post(client, "#Books/札记/grammar 一")
    _post(client, "#Books/札记/《论语》 二")
    _post(client, "#日记 三")

    parent = client.get("/api/scratch", params={"tag": "Books"}).json()
    assert parent["total"] == 2
    assert parent["all"] == 3
    assert all("Books" in one["body"] for one in parent["notes"])
    # 标签树永远按全部碎片算：点进去之后左栏不能只剩这一支
    assert {one["path"] for one in parent["tags"]} == {"Books", "日记"}

    leaf = client.get("/api/scratch", params={"tag": "Books/札记"}).json()
    assert leaf["total"] == 2
    none = client.get("/api/scratch", params={"tag": "没有这一支"}).json()
    assert none["total"] == 0 and none["notes"] == []


def test_tag_parsing_stops_at_punctuation():
    assert scratch_tags.parse("见 #语法。还有 #Books/札记，和 #英语（语法）") == [
        "语法",
        "Books/札记",
        "英语",
    ]
    # 同一句里重复只算一次；空标签不算
    assert scratch_tags.parse("#A #A # #") == ["A"]
    assert scratch_tags.under("Books", "Books/札记/grammar") is True
    assert scratch_tags.under("Books", "Bookshop") is False


def test_the_llm_can_read_fragments(client, db_session):  # noqa: ANN001
    """`read_scratchpad`：只读、在「碎片」那一组，能按标签收窄，返回原话与时间。"""
    _post(client, "#Books/札记/grammar 逗号在引号里面")
    _post(client, "#日记 今天没睡好")

    assert tools.group_of("read_scratchpad") == "scratch"
    assert tools.access_of("read_scratchpad") == "read"

    ok, payload = tools.call(db_session, "read_scratchpad", {"limit": 10})
    assert ok, payload
    assert payload["total"] == 2
    # 从新到旧：最近记的在最前面
    assert [one["body"].split(" ")[0] for one in payload["notes"]] == [
        "#日记",
        "#Books/札记/grammar",
    ]
    assert payload["notes"][1]["tags"] == ["Books/札记/grammar"]
    assert payload["notes"][0]["at"], "要给模型一个能写进回答的时间"

    ok, narrowed = tools.call(db_session, "read_scratchpad", {"tag": "Books"})
    assert ok, narrowed
    assert narrowed["matched"] == 1
    assert "引号" in narrowed["notes"][0]["body"]


def test_scratch_is_user_data_that_travels_encrypted(client):  # noqa: ANN001
    """换台机器要还在：归 user、进同步清单、进首次全量拉取、且是加密表。"""
    assert datapack.BOUNDARY["scratch"] == "user"
    assert "scratch" in datapack.USER_TABLES
    assert "scratch" in sync.SYNC_TABLES
    # `_full_pull` 只读 PULL_ORDER：漏了它，新机器上这一栏就永远是空的
    assert "scratch" in sync.PULL_ORDER
    # 随手记的东西是最私人那一类，与对话正文同一档
    assert "scratch" in sync.PRIVATE_TABLES
