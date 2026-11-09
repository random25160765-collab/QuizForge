"""计划书：每份任务计划配套的那份 Markdown（用户："和 coding agent 的 plan 一样"）。

这里盯的是那条分界：**条目是结构化的真相，文档是自由书写的工作文档**。

* 建计划就播下第一版（标题 / 目标 / 条目）；
* 条目勾选 / 改期 / 改名时，只刷新文档里标记出来的**活清单**，
  标记之外的正文一个字都不动；
* 文档可以被整份重写（AI 接管），那时活清单同步就停 —— 不是故障；
* 读写两条路（HTTP 与对话工具）共用同一份 `app/plan_doc.py`。
"""

from __future__ import annotations

from app import plan_doc, tools
from app.models import Plan


def _make_plan(client):  # noqa: ANN001, ANN202
    response = client.post(
        "/api/plans",
        json={
            "title": "两周吃透 attention",
            "note": "能不看笔记手推一遍 scaled dot-product",
            "items": [
                {"title": "读 3.1 节", "dueDate": "2026-11-09"},
                {"title": "手推 QKV"},
            ],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["plan"]


def test_creating_a_plan_seeds_a_markdown_doc(client):  # noqa: ANN001
    plan = _make_plan(client)
    assert plan["docChars"] > 0, "计划建好了却没有配套文档"

    doc = client.get(f"/api/plans/{plan['id']}/doc").json()
    assert doc["title"] == "两周吃透 attention"
    text = doc["doc"]
    assert text.startswith("# 两周吃透 attention")
    assert "> 能不看笔记手推一遍 scaled dot-product" in text
    assert "## 待办" in text
    assert "- [ ] 读 3.1 节（2026-11-09）" in text
    assert "- [ ] 手推 QKV" in text
    assert plan_doc.ITEMS_START in text and plan_doc.ITEMS_END in text

    # 列表接口只给长度，不把全文塞进每一份计划
    listed = client.get("/api/plans").json()["plans"]
    one = next(item for item in listed if item["id"] == plan["id"])
    assert one["docChars"] == len(text)


def test_item_changes_refresh_only_the_live_block(client):  # noqa: ANN001
    """勾选一条：文档里的清单跟着变，人写的那段正文原样保留。"""
    plan = _make_plan(client)
    item_id = plan["items"][0]["id"]

    original = client.get(f"/api/plans/{plan['id']}/doc").json()["doc"]
    custom = original.replace(
        "（这里写目标、拆解思路与进展；条目勾选以计划卡为准。）",
        "这条是 AI / 我自己写的思路，系统不许动。\n\n- 第二步先做 QKV",
    )
    assert client.put(f"/api/plans/{plan['id']}/doc", json={"doc": custom}).status_code == 200

    client.patch(f"/api/plan-items/{item_id}", json={"done": True})
    after = client.get(f"/api/plans/{plan['id']}/doc").json()["doc"]
    assert "- [x] 读 3.1 节（2026-11-09）" in after, "勾选没有反映到计划书"
    assert "这条是 AI / 我自己写的思路，系统不许动。" in after
    assert "- 第二步先做 QKV" in after

    # 新加一条、删掉一条，清单同样跟着走
    added = client.post(
        "/api/plan-items", json={"planId": plan["id"], "title": "整理错题"}
    ).json()["item"]
    assert "- [ ] 整理错题" in client.get(f"/api/plans/{plan['id']}/doc").json()["doc"]
    client.delete(f"/api/plan-items/{added['id']}")
    assert "整理错题" not in client.get(f"/api/plans/{plan['id']}/doc").json()["doc"]


def test_a_fully_rewritten_doc_stops_live_sync(client):  # noqa: ANN001
    """AI 整份重写（连标记一起没写）就是自己接管文档 —— 条目变化不再改它。"""
    plan = _make_plan(client)
    client.put(f"/api/plans/{plan['id']}/doc", json={"doc": "# 我自己接管了\n"})
    client.patch(f"/api/plan-items/{plan['items'][0]['id']}", json={"done": True})
    assert client.get(f"/api/plans/{plan['id']}/doc").json()["doc"] == "# 我自己接管了\n"


def test_a_proposal_carries_its_draft_doc(client):  # noqa: ANN001
    """提案卡上先展开正文（用户："我看正文，和它讨论，直到没问题再 accept"）。

    所以 `propose_plan` 要把 `doc` 原样放进 payload；接受时随 `/plans` 落库，
    并补上活清单标记 —— 否则条目以后的变动同步不进这份文档。
    """
    out = tools.propose_plan(
        None,
        {
            "title": "两周吃透 attention",
            "doc": "# 先读综述，再手推\n\n验收：不看笔记推一遍。",
            "items": [{"title": "读 3.1 节"}],
        },
    )
    proposal = out["proposal"]
    assert proposal["doc"].startswith("# 先读综述")
    assert "验收" in proposal["doc"]

    created = client.post(
        "/api/plans",
        json={
            "title": proposal["title"],
            "doc": proposal["doc"],
            "items": proposal["items"],
        },
    ).json()["plan"]
    doc = client.get(f"/api/plans/{created['id']}/doc").json()["doc"]
    assert doc.startswith("# 先读综述")
    assert plan_doc.ITEMS_START in doc, "接受之后要补上活清单，否则条目变化不同步"
    assert "- [ ] 读 3.1 节" in doc

    # 没写正文时仍有一版可读的兜底（标题 + 目标 + 清单），不是空卡
    fallback = tools.propose_plan(None, {"title": "兜底", "items": [{"title": "一条"}]})
    assert fallback["proposal"]["doc"].startswith("# 兜底")
    assert "一条" in fallback["proposal"]["doc"]


def test_old_plan_gets_a_doc_on_first_read(client, db_session):  # noqa: ANN001
    """加列之前建的计划（doc 为空）：第一次读就补上第一版，不能让界面看空。"""
    plan = _make_plan(client)
    row = db_session.get(Plan, plan["id"])
    row.doc = ""
    db_session.commit()

    doc = client.get(f"/api/plans/{plan['id']}/doc").json()
    assert doc["doc"].startswith("# 两周吃透 attention")
    assert "- [ ] 读 3.1 节" in doc["doc"]


def test_doc_write_is_full_replacement_and_capped(client):  # noqa: ANN001
    plan = _make_plan(client)
    assert client.put(f"/api/plans/{plan['id']}/doc", json={"doc": 123}).status_code == 400
    too_long = "x" * (plan_doc.DOC_MAX + 1)
    response = client.put(f"/api/plans/{plan['id']}/doc", json={"doc": too_long})
    assert response.status_code == 400
    assert "计划书不是资料库" in response.json()["detail"]


def test_plan_doc_tools_share_the_same_document(client, db_session):  # noqa: ANN001
    plan = _make_plan(client)
    ok, payload = tools.call(db_session, "read_plan_doc", {"planId": plan["id"]})
    assert ok, payload
    assert "读 3.1 节" in payload["doc"]
    assert payload["itemsBlock"] is True

    # 追加：写进展，不动条目
    ok, payload = tools.call(
        db_session,
        "update_plan_doc",
        {"planId": plan["id"], "append": "## 进展\n\n- 3.1 节读完了"},
    )
    assert ok, payload
    assert payload["mode"] == "append"
    body = client.get(f"/api/plans/{plan['id']}/doc").json()["doc"]
    assert "## 进展" in body and "3.1 节读完了" in body

    # 改一段：old 必须逐字存在
    ok, payload = tools.call(
        db_session,
        "update_plan_doc",
        {"planId": plan["id"], "old": "3.1 节读完了", "new": "3.1 节读完，记了三页笔记"},
    )
    assert ok, payload
    assert "记了三页笔记" in client.get(f"/api/plans/{plan['id']}/doc").json()["doc"]

    ok, missing = tools.call(
        db_session, "update_plan_doc", {"planId": plan["id"], "old": "不存在的原文", "new": "x"}
    )
    assert ok and "找不到" in missing["error"], missing

    # 整份替换
    ok, payload = tools.call(
        db_session, "update_plan_doc", {"planId": plan["id"], "doc": "# 新计划书\n"}
    )
    assert ok and payload["mode"] == "replace"
    assert client.get(f"/api/plans/{plan['id']}/doc").json()["doc"] == "# 新计划书\n"


def test_plan_doc_tools_refuse_a_missing_plan(db_session):  # noqa: ANN001
    ok, payload = tools.call(db_session, "read_plan_doc", {"planId": "nope"})
    assert ok and "没有这份计划" in payload["error"]
    ok, payload = tools.call(db_session, "update_plan_doc", {"planId": "nope", "doc": "# x"})
    assert ok and "没有这份计划" in payload["error"]
