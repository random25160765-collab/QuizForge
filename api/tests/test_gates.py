"""质量门禁：过了才算做完，没过要留下痕迹（并进下一次重排的队首）。

判据只有两条，别的都是细节：

* **没通过不许把条目勾上**（`done` 保持 False）；
* **没通过也要落库**，否则"下面先补哪一条"就没有依据 —— 门禁就成了摆设。

再加一条扩展性：现场注册一个新门禁，`/api/gates/prepare` 就该认它，
不用碰路由与界面（界面只认 `kind`）。
"""

from __future__ import annotations

from app import gates, scheduler
from app.models import Question


def _item(client, **raw):  # noqa: ANN001, ANN003
    res = client.post("/api/plans", json={"title": "考试周", "items": [raw]})
    assert res.status_code == 200, res.text
    return res.json()["plan"]["items"][0]


def _prepare(client, item_id: str) -> dict:  # noqa: ANN001
    res = client.post("/api/gates/prepare", json={"itemId": item_id})
    assert res.status_code == 200, res.text
    return res.json()


def _submit(client, item_id: str, answer: dict) -> dict:  # noqa: ANN001
    res = client.post("/api/gates/submit", json={"itemId": item_id, "answer": answer})
    assert res.status_code == 200, res.text
    return res.json()


def test_the_registry_and_default_mapping_are_readable(client):  # noqa: ANN001
    body = client.get("/api/gates").json()
    assert {"quiz", "recall", "explain", "artifact", "none"} <= {one["key"] for one in body["gates"]}
    assert body["defaults"]["science"] == "quiz"
    assert body["defaults"]["other"] == ""


def test_quiz_gate_fails_you_then_passes_you(client, db_session, imported_bank):  # noqa: ANN001
    item = _item(client, title="线代复习", estimateMin=60, domain="science")
    prepared = _prepare(client, item["id"])
    assert prepared["gate"] == "quiz"
    assert prepared["questions"], "题库里有题，抽不出来就是抽题逻辑坏了"
    assert "answer" not in prepared["questions"][0], "卷子不能带答案"

    failed = _submit(client, item["id"], {"choices": {}})
    assert failed["passed"] is False
    assert failed["item"]["done"] is False, "没过就不该把条目勾上"
    assert failed["gateRun"]["payload"]["wrong"], "判分要留下依据"

    choices = {
        one["id"]: db_session.get(Question, one["id"]).payload["answer"]
        for one in prepared["questions"]
    }
    passed = _submit(client, item["id"], {"choices": choices})
    assert passed["passed"] is True
    assert passed["item"]["done"] is True

    history = client.get(f"/api/gates/history?itemId={item['id']}").json()["runs"]
    assert [one["passed"] for one in history] == [True, False], "两次都要留档，包括没过的"


def test_a_failed_gate_puts_the_item_earlier_next_time(client, db_session):  # noqa: ANN001
    """门禁不是打分就完了：没过的那条会变成下一次重排的 `priority_boost`。"""
    item = _item(client, title="推导训练", estimateMin=60, domain="science")
    assert _submit(client, item["id"], {"choices": {}})["passed"] is False

    si = scheduler.build_input(db_session, days=3)
    boosts = [one for one in si.constraints if one.rule == "priority_boost"]
    assert any(one.params.get("taskId") == item["id"] for one in boosts)


def test_recall_gate_needs_a_real_retelling(client):  # noqa: ANN001
    item = _item(client, title="复述一遍", estimateMin=30, domain="art")
    prepared = _prepare(client, item["id"])
    assert prepared["gate"] == "recall" and prepared["kind"] == "text"
    assert prepared["minChars"] > 0

    too_short = _submit(client, item["id"], {"text": "记住了"})
    assert too_short["passed"] is False

    good = _submit(client, item["id"], {"text": "先说结论，再说为什么。" * 12, "selfRating": 4})
    assert good["passed"] is True


def test_self_rating_below_three_is_not_a_pass(client):  # noqa: ANN001
    item = _item(client, title="讲解一次", estimateMin=30, domain="art", gate="explain")
    honest = _submit(
        client,
        item["id"],
        {"text": "我大概知道它是在讲一个调度问题，但细节说不清。" * 6, "selfRating": 2},
    )
    assert honest["passed"] is False, "自己都觉得没懂，就不该算过"


def test_artifact_gate_is_the_default_for_skills(client):  # noqa: ANN001
    item = _item(client, title="写个小工具", estimateMin=45, domain="skill")
    prepared = _prepare(client, item["id"])
    assert prepared["gate"] == "artifact" and prepared["kind"] == "text"

    body = "print('hello')\n" * 10
    assert _submit(client, item["id"], {"text": body})["passed"] is True


def test_items_without_a_gate_just_finish(client):  # noqa: ANN001
    """`other` 域默认没有门禁：别拦着人把事勾掉。"""
    item = _item(client, title="把书还了", estimateMin=15)
    prepared = _prepare(client, item["id"])
    assert prepared["gate"] == "none" and prepared["passed"] is True
    assert _submit(client, item["id"], {})["passed"] is True


def test_a_new_gate_plugs_in_without_touching_core(client):  # noqa: ANN001
    """以后加"拍一张笔记"这种门禁：只注册，不改路由与界面。"""

    class PhotoGate(gates.Gate):
        key = "test_photo"
        label = "拍一张笔记"
        kind = "text"
        min_chars = 5

        def prepare(self, db, item, event=None):  # noqa: ANN001, ANN201
            return {**self._base(item), "prompt": "把你手写的推导拍下来，写一句它证明了什么"}

        def verify(self, db, item, event, answer):  # noqa: ANN001, ANN201
            ok = len(str((answer or {}).get("text") or "").strip()) >= self.min_chars
            return gates.GateResult(ok, 1.0 if ok else 0.0, "记下了" if ok else "太短")

    gates.register(PhotoGate())
    try:
        item = _item(client, title="实验记录", estimateMin=30, gate="test_photo")
        prepared = _prepare(client, item["id"])
        assert prepared["gate"] == "test_photo"
        assert prepared["prompt"]
        assert _submit(client, item["id"], {"text": "证明了交换律"})["passed"] is True
    finally:
        gates.GATES.pop("test_photo", None)
