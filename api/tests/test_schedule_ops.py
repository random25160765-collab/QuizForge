"""重排 / 撤销：谁说了算，以及撤销能不能真的回到原样。

这一层最容易出的不是"排得不好"，而是**把不该动的东西动了**。

* 课表（订阅来的）与钉住（`locked`）的块是事实，重排一块都不许碰；
* 用户手动改过的块（`detached`）算他的，重排也不许抹；
* 重排只回收**上一轮求解器排的**块，撤销只回滚**这一次**动过的行。

再补一条容易忽略的：连着重排两次不该让块来回跳（否则"重排"按钮就是个老虎机）。
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

from app.models import ScheduleEvent


def _plan(client, items: list[dict]) -> dict:  # noqa: ANN001
    res = client.post("/api/plans", json={"title": "考试周", "items": items})
    assert res.status_code == 200, res.text
    return res.json()["plan"]


def _events(client, days: int = 7) -> list[dict]:  # noqa: ANN001
    now = datetime.now().astimezone()
    begin = datetime.combine(now.date(), time(0), tzinfo=now.tzinfo)
    res = client.get(
        "/api/schedule",
        params={"from": begin.isoformat(), "to": (begin + timedelta(days=days)).isoformat()},
    )
    assert res.status_code == 200, res.text
    return res.json()["events"]


def _replan(client, **body) -> dict:  # noqa: ANN001
    res = client.post("/api/schedule/replan", json={"horizonDays": 5, **body})
    assert res.status_code == 200, res.text
    return res.json()


def test_replan_only_proposes_until_you_apply(client):  # noqa: ANN001
    """默认**不落库**：对话里那张整周卡就是先看不写。"""
    _plan(
        client,
        [
            {"title": "推导训练", "estimateMin": 120, "focus": "deep", "domain": "science"},
            {"title": "一整天的大作业", "estimateMin": 300, "splittable": False},
        ],
    )

    body = _replan(client)
    assert body["applied"] is False
    assert body["runId"] is None
    assert body["blocks"], "五个整天排不进两块任务？"
    assert body["diff"]["counts"]["created"] == len(body["blocks"])
    assert _events(client) == [], "只算不写，日历上不该多出块"

    reasons = [one["reason"] for one in body["unscheduled"]]
    assert reasons and all(reasons), "排不进去的必须给原因"
    assert any("240" in one for one in reasons)


def test_apply_writes_blocks_and_undo_takes_them_back(client):  # noqa: ANN001
    _plan(client, [{"title": "推导训练", "estimateMin": 120, "focus": "deep", "domain": "science"}])

    body = _replan(client, apply=True)
    assert body["applied"] is True
    assert body["runId"]

    events = _events(client)
    assert events
    assert {one["runId"] for one in events} == {body["runId"]}
    assert {one["id"] for one in events} == {
        one["id"] for one in body["diff"]["created"] + body["diff"]["moved"]
    }

    undo = client.post(f"/api/schedule/runs/{body['runId']}/undo")
    assert undo.status_code == 200, undo.text
    assert undo.json()["removed"] == len(events)
    assert _events(client) == [], "撤销之后日历该回到排程之前"
    assert client.post(f"/api/schedule/runs/{body['runId']}/undo").status_code == 409


def test_a_course_and_a_pinned_block_are_never_moved(client, db_session):  # noqa: ANN001
    """课表是别处的事实，钉住是人的决定：两样都不能进重排的回收桶。"""
    day = (datetime.now().astimezone() + timedelta(days=1)).date()
    local = datetime.combine(day, time(10, 0), tzinfo=datetime.now().astimezone().tzinfo)

    course = ScheduleEvent(
        sub_id="sub-1",
        uid="course-1",
        occurrence="20261110T020000Z",
        title="高等数学",
        start_at=local.astimezone(UTC),
        end_at=(local + timedelta(minutes=90)).astimezone(UTC),
    )
    pinned = ScheduleEvent(
        title="钉住的复习",
        start_at=(local + timedelta(hours=3)).astimezone(UTC),
        end_at=(local + timedelta(hours=4)).astimezone(UTC),
        locked=True,
    )
    db_session.add_all([course, pinned])
    db_session.commit()
    course_id, pinned_id, pinned_start = course.id, pinned.id, local + timedelta(hours=3)

    _plan(client, [{"title": "习题", "estimateMin": 90, "domain": "science"}])
    body = _replan(client, apply=True)
    assert body["applied"]

    events = {one["id"]: one for one in _events(client)}
    assert course_id in events, "课表块被重排删了"
    assert pinned_id in events, "钉住的块被重排删了"
    assert datetime.fromisoformat(events[pinned_id]["startAt"]) == pinned_start.astimezone(UTC)
    assert events[course_id]["locked"] is False and events[pinned_id]["locked"] is True

    # 新排的块不许压在这两块上
    for one in events.values():
        if one["id"] in (course_id, pinned_id):
            continue
        start = datetime.fromisoformat(one["startAt"])
        end = datetime.fromisoformat(one["endAt"])
        for busy in (course, pinned):
            assert end <= busy.start_at or start >= busy.end_at


def test_a_block_the_user_edited_is_not_recycled(client):  # noqa: ANN001
    """拖过一次的块就归用户：再重排时它既不被删，也不被当成障碍以外的别的东西。"""
    _plan(client, [{"title": "推导训练", "estimateMin": 120, "focus": "deep"}])
    body = _replan(client, apply=True)
    events = _events(client)
    assert events

    edited = events[0]
    patched = client.patch(f"/api/schedule/{edited['id']}", json={"title": "我自己改的名字"})
    assert patched.status_code == 200, patched.text
    assert patched.json()["event"]["detached"] is True

    _replan(client, apply=True)
    after = {one["id"]: one for one in _events(client)}
    assert edited["id"] in after, "用户改过的块被下一次重排抹掉了"
    assert after[edited["id"]]["title"] == "我自己改的名字"


def test_replanning_twice_does_not_thrash_the_same_blocks(client):  # noqa: ANN001
    """同一份输入连着重排两次，第二次应该**什么都不改**（否则按钮就是老虎机）。"""
    _plan(
        client,
        [
            {"title": "线代", "estimateMin": 90, "domain": "science"},
            {"title": "读书", "estimateMin": 60, "domain": "art"},
        ],
    )
    first = _replan(client, apply=True)
    before = {one["id"] for one in _events(client)}
    assert first["diff"]["counts"]["created"] == len(before)

    second = _replan(client, apply=True)
    assert second["diff"]["counts"] == {"created": 0, "moved": 0, "deleted": 0, "unchanged": len(before)}
    assert {one["id"] for one in _events(client)} == before


def test_schedule_week_proposes_but_does_not_write(client, db_session):  # noqa: ANN001
    """对话侧的整周排程只出卡：`schedule_week` 一个字都不许落库。"""
    from app import tools

    _plan(client, [{"title": "推导训练", "estimateMin": 90, "focus": "deep", "domain": "science"}])
    body = tools.schedule_week(db_session, {"horizonDays": 5})

    assert body["proposal"]["kind"] == "plan.scheduleBatch"
    assert body["proposal"]["blocks"], "五个整天排不进一条 90 分钟的任务？"
    assert all(one["reason"] for one in body["proposal"]["blocks"]), "每块都要能回答为什么排在这"
    assert _events(client) == [], "提案阶段不许写日历"


def test_get_energy_summarizes_today(client, db_session):  # noqa: ANN001
    from app import tools

    body = tools.get_energy(db_session)
    assert 0.0 <= body["energy"]["now"] <= 1.0
    assert body["deepBudgetMin"] >= 0
    assert body["targets"]["bed"]
    assert "别把" in body["note"]


def test_propose_plan_carries_the_scheduling_metadata(client):  # noqa: ANN001
    """计划里就得写清专注度与学科：求解器靠它决定排上午还是晚上。"""
    from app import tools

    out = tools.propose_plan(
        None,
        {
            "title": "两周吃透 attn",
            "items": [
                {"title": "推一遍 attention", "focus": "deep", "domain": "science", "points": ["attn.softmax"]},
                {"title": "读综述", "focus": "light", "domain": "art"},
            ],
        },
    )
    items = out["proposal"]["items"]
    assert items[0]["focus"] == "deep" and items[0]["domain"] == "science"
    assert items[0]["points"] == ["attn.softmax"]

    bad = tools.propose_plan(None, {"items": [{"title": "x", "focus": "hardcore"}]})
    assert "focus" in bad["error"]


def test_adding_one_task_does_not_reshuffle_the_week(client):  # noqa: ANN001
    """重排要**保守**：加一条待办，不该让已经排好的十几块全部搬家。

    钉子：上一轮的位置在目标函数里有一条软偏好（`ScheduleInput.previous`）。
    没有它，求解器在等价的排法之间随便挑，用户每次点「重排」都像洗牌。
    """
    _plan(
        client,
        [
            {"title": "线代", "estimateMin": 90, "domain": "science"},
            {"title": "读书", "estimateMin": 60, "domain": "art"},
        ],
    )
    first = _replan(client, apply=True)
    assert first["diff"]["counts"]["created"] >= 2
    before = {one["id"]: one["startAt"] for one in _events(client)}

    _plan(client, [{"title": "补一页笔记", "estimateMin": 45, "domain": "art"}])
    second = _replan(client, apply=True)

    assert second["diff"]["counts"]["moved"] == 0, "加一条待办，老块不该跟着搬家"
    after = {one["id"]: one["startAt"] for one in _events(client)}
    for key, start in before.items():
        assert after.get(key) == start, "老块的 id 与时间都该原样保留"


def test_undo_puts_back_the_blocks_a_replan_deleted(client):  # noqa: ANN001
    """撤销不靠全库回滚：它按快照把被这次重排删掉的行原样写回来。"""
    plan = _plan(client, [{"title": "推导训练", "estimateMin": 60, "focus": "deep"}])
    first = _replan(client, apply=True)
    original = {one["id"]: one for one in _events(client)}
    assert original

    item_id = plan["items"][0]["id"]
    done = client.patch(f"/api/plan-items/{item_id}", json={"done": True})
    assert done.status_code == 200, done.text

    second = _replan(client, apply=True)
    assert second["diff"]["counts"]["deleted"] == len(original)
    assert _events(client) == [], "待办已经做完了，旧的排程块应该被回收"

    undo = client.post(f"/api/schedule/runs/{second['runId']}/undo")
    assert undo.status_code == 200, undo.text
    assert undo.json()["restored"] == len(original)
    restored = {one["id"]: one for one in _events(client)}
    assert set(restored) == set(original), "撤销之后 id 与时间都该是原来那批"
    for key, one in original.items():
        assert restored[key]["startAt"] == one["startAt"]

    # 第一次重排仍然可以独立撤销：两次重排各记各的账
    assert client.post(f"/api/schedule/runs/{first['runId']}/undo").status_code == 200
