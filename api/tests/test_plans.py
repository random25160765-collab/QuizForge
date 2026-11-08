"""计划与日程：两张表、两组接口、一件事。

用户定的形状是 **"计划是计划，日程是日程"**：条目可以一直没有日期，
日历上的时间块才是"哪个时段做什么"。这里盯的是这条分界最容易塌掉的几处：

* **条目带 dueDate 不等于进了日程** —— 建完计划，日程里必须还是空的；
* **凭条点两次只建一份**（`proposalId` 唯一），这是"确认"这个动作可重复的前提；
* **时间窗按区间重叠查** —— 跨午夜的那一块，起点在昨天但它今天仍占着两小时；
* **删计划要把排出去的时间块一起删**，但手动建的事件不许跟着没；
* **`BOUNDARY` 里这三张表都是 user**（否则用户包里带不走他的计划）；
* **订阅（iCalendar）是"别处的事实"**：重新同步要对得上（不多不少），
  而用户自己挪过的那一块不许被覆盖 —— 见文件末尾那几条。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app import datapack, ics


def _iso(delta_minutes: int = 0) -> str:
    return (datetime.now(UTC) + timedelta(minutes=delta_minutes)).isoformat()


def test_due_date_is_not_a_schedule(client):  # noqa: ANN001
    """建一份带日期的计划：条目有日子，但**日程里什么都没有**。

    这是整套设计的判据。如果这里能查出东西，说明"计划"与"日程"又被合并了。
    """
    created = client.post(
        "/api/plans",
        json={
            "title": "两周吃透 attention",
            "items": [{"title": "读 3.1 节", "dueDate": "2026-11-09", "estimateMin": 45}],
        },
    ).json()
    plan = created["plan"]
    assert created["created"] is True
    assert plan["items"][0]["dueDate"] == "2026-11-09"

    window = client.get(
        "/api/schedule",
        params={"from": "2026-11-01T00:00:00Z", "to": "2026-12-01T00:00:00Z"},
    ).json()
    assert window["events"] == [], "条目带了日期就被当成日程了"


def test_proposal_id_is_idempotent(client):  # noqa: ANN001
    """同一张凭条点两次：第二次数 `created: false`，且不会多出一份计划。"""
    body = {
        "proposalId": "proposal-abc",
        "title": "两天弄明白 QKV",
        "source": "chat",
        "items": [{"title": "手推一遍 scaled dot-product"}],
    }
    first = client.post("/api/plans", json=body).json()
    second = client.post("/api/plans", json=body).json()

    assert first["created"] is True
    assert second["created"] is False
    assert second["plan"]["id"] == first["plan"]["id"]
    assert [one["title"] for one in second["plan"]["items"]] == ["手推一遍 scaled dot-product"]

    listed = client.get("/api/plans").json()["plans"]
    assert len([one for one in listed if one["proposalId"] == "proposal-abc"]) == 1

    # 回读：凭条刷新之后靠它认"已经加过了"
    found = client.get("/api/plans", params={"proposalId": "proposal-abc"}).json()["plan"]
    assert found["id"] == first["plan"]["id"]


def test_schedule_range_uses_overlap_not_start(client):  # noqa: ANN001
    """时间窗按**区间重叠**查：起点在窗前、尾巴伸进来的那一块也要算。"""
    inside = client.post(
        "/api/schedule",
        json={"title": "窗内", "startAt": _iso(60), "endAt": _iso(120)},
    ).json()["event"]

    # 起点在窗之前 30 分钟、结束在窗内
    window_from = (datetime.now(UTC) + timedelta(minutes=30)).isoformat()
    window_to = (datetime.now(UTC) + timedelta(minutes=600)).isoformat()
    crossed = client.post(
        "/api/schedule",
        json={
            "title": "跨界",
            "startAt": (datetime.now(UTC) - timedelta(minutes=30)).isoformat(),
            "endAt": (datetime.now(UTC) + timedelta(minutes=45)).isoformat(),
        },
    ).json()["event"]

    events = client.get(
        "/api/schedule", params={"from": window_from, "to": window_to}
    ).json()["events"]
    ids = {one["id"] for one in events}
    assert inside["id"] in ids
    assert crossed["id"] in ids, "起点在窗之前就不算了吗"


def test_deleting_a_plan_takes_its_blocks_not_the_manual_ones(client):  # noqa: ANN001
    """删计划：连带它条目排出去的块；手动建的事件必须留着。"""
    plan = client.post(
        "/api/plans", json={"title": "t", "items": [{"title": "读"}]}
    ).json()["plan"]
    item_id = plan["items"][0]["id"]

    from_plan = client.post(
        "/api/schedule",
        json={"itemId": item_id, "startAt": _iso(60), "endAt": _iso(120)},
    ).json()["event"]
    manual = client.post(
        "/api/schedule",
        json={"title": "组会", "startAt": _iso(180), "endAt": _iso(240)},
    ).json()["event"]

    removed = client.delete(f"/api/plans/{plan['id']}").json()
    assert removed["ok"] is True
    assert removed["deleted"]["items"] == 1
    assert removed["deleted"]["events"] == 1

    window = client.get(
        "/api/schedule",
        params={"from": _iso(-120), "to": _iso(600)},
    ).json()["events"]
    ids = {one["id"] for one in window}
    assert from_plan["id"] not in ids
    assert manual["id"] in ids, "手动建的事件不该被计划带走"


def test_items_carry_scheduling_metadata(client):  # noqa: ANN001
    """专注度 / 学科 / 可拆 / 单块上限 / 知识点：老条目读出来是默认值，新条目能写。

    这些字段是给求解器与门禁用的，所以两个方向都要盯：写进去要原样回来，
    写错（"深度"这种中文）要**落回默认**，不能让一个错词悄悄把排程带跑。
    """
    plan = client.post(
        "/api/plans",
        json={
            "title": "排程元数据",
            "items": [
                {
                    "title": "读《纯粹理性批判》",
                    "focus": "deep",
                    "domain": "art",
                    "estimateMin": 90,
                    "points": ["phil-kant-1", "phil-kant-1", ""],
                }
            ],
        },
    ).json()["plan"]
    item = plan["items"][0]
    assert item["focus"] == "deep"
    assert item["domain"] == "art"
    assert item["splittable"] is True
    assert item["points"] == ["phil-kant-1"], "知识点要去空去重"

    plain = client.post(
        "/api/plan-items", json={"planId": plan["id"], "title": "随手一条"}
    ).json()["item"]
    assert (plain["focus"], plain["domain"]) == ("medium", "other")
    assert plain["splittable"] is True
    assert plain["maxChunkMin"] == 0
    assert plain["gate"] == ""

    fixed = client.patch(
        f"/api/plan-items/{plain['id']}",
        json={"focus": "深度", "domain": "理科", "maxChunkMin": 45, "gate": "quiz", "splittable": False},
    ).json()["item"]
    assert fixed["focus"] == "medium", "认不出的专注度要落回默认"
    assert fixed["domain"] == "other"
    assert fixed["maxChunkMin"] == 45
    assert fixed["gate"] == "quiz"
    assert fixed["splittable"] is False


def test_plan_tables_are_user_data():
    """三张表都属于用户：换台机器打开，他的计划得还在（用户包要带走它们）。"""
    for table in ("plans", "plan_items", "schedule_events"):
        assert datapack.BOUNDARY.get(table) == "user", f"{table} 没登记成用户数据"
        assert table in datapack.USER_TABLES


# ============================================================================ 订阅（iCalendar）
#
# 用户的原话是"我们的日历需要支持标准协议"。这里的样例就是学校课表那个订阅
# 的形状：UTC 时刻 + `FREQ=WEEKLY;UNTIL=`，外加一条 EXDATE 与折行。

FEED = "\r\n".join(
    [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//test//test//CN",
        "X-WR-CALNAME:测试课表",
        "X-WR-TIMEZONE:Asia/Shanghai",
        "BEGIN:VEVENT",
        "UID:course-1@test",
        "SUMMARY:高等数学",
        "LOCATION:A410",
        "DESCRIPTION:任课教师：张老师\\n",
        "DTSTART:20261109T060000Z",
        "DTEND:20261109T074000Z",
        "RRULE:FREQ=WEEKLY;COUNT=3",
        # 11 月 16 日那次停课
        "EXDATE:20261116T060000Z",
        "BEGIN:VALARM",
        "ACTION:DISPLAY",
        "SUMMARY:别把闹钟当课程名",
        "TRIGGER:-PT15M",
        "END:VALARM",
        "END:VEVENT",
        "END:VCALENDAR",
        "",
    ]
)


def test_ics_expands_weekly_and_honours_count_and_exdate():
    """COUNT 数的是**规则生成了几次**：被 EXDATE 排掉的那次也算一次。

    按"排掉之后还剩 3 次"去理解，就会凭空多出一节课 —— 而课表是要拿去上课的。
    """
    cal = ics.parse(FEED)
    assert cal.name == "测试课表"
    assert len(cal.events) == 1

    one = cal.events[0]
    assert one.summary == "高等数学", "VALARM 里的 SUMMARY 串味了"
    assert one.location == "A410"
    assert "张老师" in one.description

    blocks = one.occurrences(datetime(2030, 1, 1, tzinfo=UTC))
    assert [key for _start, _end, key in blocks] == [
        "20261109T060000Z",
        "20261123T060000Z",
    ]
    assert blocks[0][0] == datetime(2026, 11, 9, 6, 0, tzinfo=UTC)
    assert blocks[0][1] - blocks[0][0] == timedelta(minutes=100)


def test_ics_reads_tzid_and_folded_lines():
    """折行先拼回去；`TZID=Asia/Shanghai` 的 14:00 就是 06:00Z。"""
    text = "\r\n".join(
        [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "BEGIN:VEVENT",
            "UID:t-1",
            "SUMMARY:信号与系统（第 3",
            " 章）",
            "DTSTART;TZID=Asia/Shanghai:20261110T140000",
            "DTEND;TZID=Asia/Shanghai:20261110T154000",
            "END:VEVENT",
            "END:VCALENDAR",
            "",
        ]
    )
    one = ics.parse(text).events[0]
    assert one.summary == "信号与系统（第 3章）"
    assert one.start == datetime(2026, 11, 10, 6, 0, tzinfo=UTC)
    assert one.end == datetime(2026, 11, 10, 7, 40, tzinfo=UTC)


def _subscribe(client, monkeypatch, url):  # noqa: ANN001, ANN202
    monkeypatch.setattr(ics, "fetch_feed", lambda _url: FEED)
    return client.post("/api/schedule/subscriptions", json={"url": url}).json()


def test_subscribing_stores_blocks_and_resync_is_stable(client, monkeypatch):  # noqa: ANN001
    token = uuid.uuid4().hex
    # 秘密只在查询串里（学校课表的形状）：路径可以照常显示，token 必须抹掉
    url = f"https://example.com/cal/course.ics?token={token}&from=quizforge"
    first = _subscribe(client, monkeypatch, url)
    assert first["created"] is True
    assert first["total"] == 2 and first["added"] == 2

    window = client.get(
        "/api/schedule",
        params={"from": "2026-11-01T00:00:00Z", "to": "2026-12-01T00:00:00Z"},
    ).json()["events"]
    assert [one["title"] for one in window] == ["高等数学", "高等数学"]
    assert {one["subId"] for one in window} == {first["subscription"]["id"]}
    assert window[0]["location"] == "A410"

    # 同一个地址再订一次：不多一份、也不重复建块
    again = _subscribe(client, monkeypatch, url)
    assert again["created"] is False
    assert again["subscription"]["id"] == first["subscription"]["id"]
    assert again["added"] == 0 and again["removed"] == 0 and again["total"] == 2

    listed = client.get("/api/schedule/subscriptions").json()["subscriptions"]
    mine = [one for one in listed if one["id"] == first["subscription"]["id"]]
    assert len(mine) == 1
    # 地址是凭据：列表里只留键名，不留值
    assert token not in mine[0]["url"]
    assert "token=…" in mine[0]["url"] and "from=…" in mine[0]["url"]

    client.delete(f"/api/schedule/subscriptions/{first['subscription']['id']}")


def test_resync_removes_gone_blocks_but_keeps_what_the_user_moved(client, monkeypatch):  # noqa: ANN001
    """课表会调课：订阅里没了的那一次要跟着没；用户挪过的那一块不许被抹平。"""
    token = uuid.uuid4().hex
    url = f"https://example.com/cal/{token}.ics"
    sub = _subscribe(client, monkeypatch, url)["subscription"]

    window = client.get(
        "/api/schedule",
        params={"from": "2026-11-01T00:00:00Z", "to": "2026-12-01T00:00:00Z"},
    ).json()["events"]
    moved, gone = window[0], window[1]

    client.patch(
        f"/api/schedule/{moved['id']}",
        json={"startAt": "2026-11-09T12:00:00Z", "endAt": "2026-11-09T13:40:00Z"},
    )

    # 11 月 23 日那次也停了
    narrowed = FEED.replace(
        "EXDATE:20261116T060000Z", "EXDATE:20261116T060000Z,20261123T060000Z"
    )
    monkeypatch.setattr(ics, "fetch_feed", lambda _url: narrowed)
    again = client.post(f"/api/schedule/subscriptions/{sub['id']}/sync").json()
    assert again["removed"] == 1
    assert again["pinned"] == 1

    left = client.get(
        "/api/schedule",
        params={"from": "2026-11-01T00:00:00Z", "to": "2026-12-01T00:00:00Z"},
    ).json()["events"]
    assert [one["id"] for one in left] == [moved["id"]]
    assert left[0]["startAt"] == "2026-11-09T12:00:00Z", "用户挪的位置被同步盖回去了"
    assert left[0]["detached"] is True
    assert gone["id"] not in {one["id"] for one in left}

    client.delete(f"/api/schedule/subscriptions/{sub['id']}")


def test_unsubscribing_takes_its_blocks_away(client, monkeypatch):  # noqa: ANN001
    token = uuid.uuid4().hex
    url = f"https://example.com/cal/{token}.ics"
    sub = _subscribe(client, monkeypatch, url)["subscription"]

    removed = client.delete(f"/api/schedule/subscriptions/{sub['id']}").json()
    assert removed["deleted"]["events"] == 2

    left = client.get(
        "/api/schedule",
        params={"from": "2026-11-01T00:00:00Z", "to": "2026-12-01T00:00:00Z"},
    ).json()["events"]
    assert [one for one in left if one["subId"] == sub["id"]] == []
    listed = client.get("/api/schedule/subscriptions").json()["subscriptions"]
    assert all(one["id"] != sub["id"] for one in listed)
