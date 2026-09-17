"""用户题单：自己出的题能增、能改、能删，而且**只属于自己**。

与公共题库分开不是洁癖：公共那张表挂着覆盖率对账、图谱与出题流水线的状态机，
用户随手出的题混进去会污染统计口径，而且删不掉（公共题只下架）。
"""

from __future__ import annotations

import uuid

PASSWORD = "password-1234"


def _register(client) -> None:  # noqa: ANN001
    client.cookies.clear()
    resp = client.post(
        "/api/auth/register",
        json={"email": f"mine-{uuid.uuid4().hex[:10]}@example.com", "password": PASSWORD},
    )
    assert resp.status_code in (200, 201), resp.text


def _csrf(client) -> dict:  # noqa: ANN001
    return {"X-CSRF-Token": client.cookies.get("qf_csrf") or ""}


def _draft(**over: object) -> dict:
    question = {
        "type": "single",
        "stem": "circular buffer 的同步靠什么？",
        "options": ["软件锁", "硬件元数据同步", "轮询", "中断"],
        "answer": "B",
        "explanation": "材料 L78：implemented in SRAM、hardware metadata synchronization。",
        "layer": "识记",
        "wing": "基础",
        "difficulty": 2,
        "topic": "tt-arch",
    }
    question.update(over)
    return question


def test_a_question_goes_in_comes_out_changes_and_goes_away(client) -> None:  # noqa: ANN001
    _register(client)
    headers = _csrf(client)

    created = client.post("/api/my/questions", json={"payload": _draft()}, headers=headers)
    assert created.status_code == 200, created.text
    row = created.json()["question"]
    assert row["id"].startswith("uq-")
    assert row["source"] == "mine"
    assert row["payload"]["stem"].startswith("circular buffer")

    listed = client.get("/api/my/questions").json()
    assert listed["count"] == 1 and listed["questions"][0]["id"] == row["id"]

    # 随出随改：改题干、改答案
    edited = client.patch(
        f"/api/my/questions/{row['id']}",
        json={"payload": _draft(stem="改过的题干", answer="C")},
        headers=headers,
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["question"]["payload"]["stem"] == "改过的题干"
    assert edited.json()["question"]["payload"]["answer"] == "C"

    gone = client.delete(f"/api/my/questions/{row['id']}", headers=headers)
    assert gone.status_code == 200, gone.text
    assert client.get("/api/my/questions").json()["count"] == 0


def test_a_question_without_a_stem_is_refused(client) -> None:  # noqa: ANN001
    _register(client)
    resp = client.post("/api/my/questions", json={"payload": {"stem": "  "}}, headers=_csrf(client))
    assert resp.status_code == 400
    assert "题干" in resp.json()["detail"]


def test_my_questions_belong_to_me_alone(client) -> None:  # noqa: ANN001
    """题单是私有的：别人既看不到、也改不动、更删不掉。"""
    _register(client)
    mine = client.post(
        "/api/my/questions", json={"payload": _draft()}, headers=_csrf(client)
    ).json()["question"]

    _register(client)  # 换个账号
    assert client.get("/api/my/questions").json()["count"] == 0
    assert (
        client.patch(
            f"/api/my/questions/{mine['id']}",
            json={"payload": _draft(stem="我改")},
            headers=_csrf(client),
        ).status_code
        == 404
    )
    assert client.delete(f"/api/my/questions/{mine['id']}", headers=_csrf(client)).status_code == 404
