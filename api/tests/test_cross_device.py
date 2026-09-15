"""跨设备正确性。

这是本次改造的规格书。旧实现让客户端上传「我这台设备看到的累计值」，
多设备下按 last-write-wins 合并 —— 一台设备离线作答后回传，若 rev 已被
另一台超过，这次作答就被**静默丢弃**。改成流水增量后，这里逐条把它钉住。

「两台设备」在测试里的表示：两个独立的 `TestClient`（各自一份 Cookie，
即同一个账号的两个会话）。
"""

from __future__ import annotations

import uuid

import pytest

from app.deps import CSRF_COOKIE

PASSWORD = "password-1234"
DAY = "2026-09-15"
T0 = 1789459200000  # 固定时间基准，避免用例之间互相影响


# ------------------------------------------------------------------ 夹具


@pytest.fixture()
def devices(engine, imported_bank):  # noqa: ANN001, ANN201
    """同一账号的两台设备（两个独立会话）。"""
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app()
    phone, laptop = TestClient(app), TestClient(app)
    email = f"xd-{uuid.uuid4().hex[:10]}@example.com"

    registered = phone.post("/api/auth/register", json={"email": email, "password": PASSWORD})
    assert registered.status_code in (200, 201), registered.text
    assert laptop.post("/api/auth/login", json={"email": email, "password": PASSWORD}).status_code == 200

    # 换个真实存在的知识点，好验证每日统计里的学科归属
    topic = imported_bank.questions[0]["topic"]
    return {"phone": phone, "laptop": laptop, "topic": topic}


def _headers(client) -> dict:  # noqa: ANN001
    return {"X-CSRF-Token": client.cookies.get(CSRF_COOKIE) or ""}


def _sync(client, **payload):  # noqa: ANN001, ANN202
    resp = client.post("/api/progress/sync", json=payload, headers=_headers(client))
    assert resp.status_code == 200, resp.text
    return resp.json()


def _snapshot(client) -> dict:  # noqa: ANN001
    resp = client.get("/api/progress")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _attempt(question_id: str, *, at: int, day: str = DAY, status: str = "correct",
             topic: str = "", score: float = 1.0, uid: str | None = None,
             response=None) -> dict:  # noqa: ANN001
    return {
        "id": uid or str(uuid.uuid4()),
        "questionId": question_id,
        "at": at,
        "day": day,
        "status": status,
        "score": score,
        "topicKey": topic,
        "response": response,
    }


# -------------------------------------------------------- 核心：同日跨设备累加


def test_same_day_two_devices_add_up(devices) -> None:  # noqa: ANN001
    """两台设备同一天各练若干题 —— 求和，不是取较大值。

    手机 10 题 + 笔记本 5 题 = 15。旧实现（逐字段取 max）这里会得到 10，
    用户会以为另一台设备上的 5 题白练了。
    """
    phone, laptop, topic = devices["phone"], devices["laptop"], devices["topic"]

    _sync(phone, attempts=[
        _attempt(f"c-{i:04d}", at=T0 + i, topic=topic, status="correct") for i in range(10)
    ])
    _sync(laptop, attempts=[
        _attempt(f"c-{i:04d}", at=T0 + 5000 + i, topic=topic, status="wrong") for i in range(10, 15)
    ])

    day = _snapshot(phone)["days"][DAY]
    assert day["answers"] == 15, "两台设备的作答必须相加"
    assert day["correct"] == 10
    # 学科归属由服务端从主题树解析，这里不断言具体 key，只确认没有丢
    assert sum(day["topics"].values()) == 15


def test_same_question_two_devices_accumulate(devices) -> None:  # noqa: ANN001
    """同一道题在两台设备上各答一次 —— 计数是 2，不是 1。"""
    phone, laptop, topic = devices["phone"], devices["laptop"], devices["topic"]

    _sync(phone, attempts=[_attempt("c-0001", at=T0, topic=topic, status="wrong")])
    _sync(laptop, attempts=[_attempt("c-0001", at=T0 + 5000, topic=topic, status="correct")])

    rec = _snapshot(laptop)["records"]["c-0001"]
    assert rec["attempts"] == 2
    assert rec["wrong"] == 1
    assert rec["correct"] == 1
    assert rec["lastStatus"] == "correct", "最近一次应当是后到的那条"


def test_resend_is_not_double_counted(devices) -> None:  # noqa: ANN001
    """「请求发出、响应丢了、客户端重发」不能重复计数。

    这是跨设备场景里最关键的一条：网络重试是常态，
    幂等交给主键约束 + 只对新插入的行累加来保证。
    """
    phone, topic = devices["phone"], devices["topic"]
    batch = [_attempt("c-0001", at=T0, topic=topic), _attempt("c-0002", at=T0 + 1, topic=topic)]

    first = _sync(phone, attempts=batch)
    second = _sync(phone, attempts=batch)  # 原样重发

    assert first["attemptsAccepted"] == 2
    assert second["attemptsAccepted"] == 0, "重发的流水不该被再接受一次"

    snapshot = _snapshot(phone)
    assert snapshot["records"]["c-0001"]["attempts"] == 1
    assert snapshot["days"][DAY]["answers"] == 2, "每日统计也不能重复累加"


def test_partial_resend_only_counts_the_new_ones(devices) -> None:  # noqa: ANN001
    """重发时夹带新流水：只有新的那条被计入。"""
    phone, topic = devices["phone"], devices["topic"]
    first = _attempt("c-0001", at=T0, topic=topic)
    second = _attempt("c-0002", at=T0 + 1, topic=topic)

    _sync(phone, attempts=[first])
    result = _sync(phone, attempts=[first, second])

    assert result["attemptsAccepted"] == 1
    assert _snapshot(phone)["days"][DAY]["answers"] == 2


# ------------------------------------------------------------ 乱序到达


def test_out_of_order_flow_does_not_rewind_last_status(devices) -> None:  # noqa: ANN001
    """离线设备补传旧流水：不 update 时间只会更早、最近一次不会倒退。

    若被倒退，界面上的判定条会显示一个已经过期的答案。
    """
    phone, topic = devices["phone"], devices["topic"]

    _sync(phone, attempts=[_attempt("c-0001", at=T0 + 10000, topic=topic, status="correct")])
    # 补传一条更早的、当时答错的流水
    _sync(phone, attempts=[_attempt("c-0001", at=T0, topic=topic, status="wrong")])

    rec = _snapshot(phone)["records"]["c-0001"]
    assert rec["attempts"] == 2
    assert rec["wrong"] == 1 and rec["correct"] == 1
    assert rec["firstAt"] == T0, "更早的时间要更新首次作答"
    assert rec["lastAt"] == T0 + 10000
    assert rec["lastStatus"] == "correct", "补传旧流水不得把最近状态改回过去"


def test_out_of_order_across_two_devices(devices) -> None:  # noqa: ANN001
    """两台设备各自补传，时间窗口最终收敛到同一组极值。"""
    phone, laptop, topic = devices["phone"], devices["laptop"], devices["topic"]

    _sync(phone, attempts=[_attempt("c-0001", at=T0 + 5000, topic=topic, status="correct")])
    _sync(laptop, attempts=[_attempt("c-0001", at=T0, topic=topic, status="wrong")])

    rec = _snapshot(phone)["records"]["c-0001"]
    assert (rec["firstAt"], rec["lastAt"]) == (T0, T0 + 5000)
    assert rec["lastStatus"] == "correct"


def test_last_status_and_response_come_from_the_same_attempt(devices) -> None:  # noqa: ANN001
    """最近一次的判定与展示的作答必须来自**同一次**作答。

    两者若各自跟着不同设备的不同次作答，界面上会出现
    「判定条说这题答对了，展示的作答却是一个错答案」这种自相矛盾。
    """
    phone, laptop, topic = devices["phone"], devices["laptop"], devices["topic"]

    _sync(phone, attempts=[_attempt("c-0001", at=T0, topic=topic, status="wrong", response="A")])
    _sync(laptop, attempts=[
        _attempt("c-0001", at=T0 + 5000, topic=topic, status="correct", response="C")
    ])

    rec = _snapshot(phone)["records"]["c-0001"]
    assert rec["lastStatus"] == "correct"
    assert rec["lastResponse"] == "C", "展示的作答要跟着最新的那次走"


def test_ungraded_draft_response_is_kept(devices) -> None:  # noqa: ANN001
    """「待批改」不产生流水，但用户填的内容必须存得住。

    这正是 `lastResponse` 除了流水之外还要允许补丁通道写它的原因。
    """
    phone = devices["phone"]
    _sync(phone, patches={"c-0001": {"_rev": 1, "lastResponse": "我写了一半"}})

    rec = _snapshot(phone)["records"]["c-0001"]
    assert rec["lastResponse"] == "我写了一半"
    assert rec["attempts"] == 0, "待批改不是作答，不该计入统计"


# ------------------------------------------------------ 计数不得来自客户端


def test_client_cannot_write_counters_via_patches(devices) -> None:  # noqa: ANN001
    """补丁通道里的计数字段一律被忽略。

    这条硬约束是整个改造的前提：只要还允许上传计数，
    一台设备的快照就会覆盖另一台的增量。
    """
    phone, topic = devices["phone"], devices["topic"]
    _sync(phone, attempts=[_attempt("c-0001", at=T0, topic=topic)])

    _sync(
        phone,
        patches={
            "c-0001": {
                "_rev": 99,
                "flagged": True,
                # 全是客户端编造的计数，必须无效
                "attempts": 999,
                "correct": 999,
                "wrong": 888,
                "lastAt": T0 + 999999,
                "lastStatus": "wrong",
            }
        },
    )

    rec = _snapshot(phone)["records"]["c-0001"]
    assert rec["flagged"] is True, "用户动作要生效"
    assert rec["attempts"] == 1, "计数只能来自流水"
    assert rec["correct"] == 1
    assert rec["wrong"] == 0
    assert rec["lastStatus"] == "correct"
    assert rec["lastAt"] == T0


def test_patch_arriving_before_flow_keeps_counters_zero(devices) -> None:  # noqa: ANN001
    """只标了星标、还没作答：记录存在，但计数是 0。"""
    phone = devices["phone"]
    _sync(phone, patches={"c-0001": {"_rev": 1, "flagged": True}})

    rec = _snapshot(phone)["records"]["c-0001"]
    assert rec["flagged"] is True
    assert rec["attempts"] == 0


def test_stale_patch_is_rejected_and_authoritative_returned(devices) -> None:  # noqa: ANN001
    """过期的补丁被拒绝，并回传权威值，让客户端跟上。"""
    phone, laptop = devices["phone"], devices["laptop"]

    _sync(phone, patches={"c-0001": {"_rev": 5, "flagged": True}})
    result = _sync(laptop, patches={"c-0001": {"_rev": 2, "flagged": False}})

    assert "c-0001" in result["patchesRejected"]
    assert result["patchesRejected"]["c-0001"]["flagged"] is True
    assert _snapshot(laptop)["records"]["c-0001"]["flagged"] is True


def test_patch_rev_merges_with_flow_created_record(devices) -> None:  # noqa: ANN001
    """同一批里先流水后补丁：补丁落在流水创建的那一行上，不互相覆盖。"""
    phone, topic = devices["phone"], devices["topic"]
    _sync(
        phone,
        attempts=[_attempt("c-0001", at=T0, topic=topic)],
        patches={"c-0001": {"_rev": 1, "flagged": True, "note": "回头再看", "streak": 2}},
    )

    rec = _snapshot(phone)["records"]["c-0001"]
    assert rec["attempts"] == 1
    assert rec["flagged"] is True
    assert rec["note"] == "回头再看"
    assert rec["streak"] == 2


# ---------------------------------------------------------------- 重置基线


def test_reset_sets_baseline_and_keeps_idempotency(devices) -> None:  # noqa: ANN001
    """重置把计数设成基线，但**不删流水**，所以重发旧流水不会又加一次。"""
    phone, topic = devices["phone"], devices["topic"]
    old = _attempt("c-0001", at=T0, topic=topic)

    _sync(phone, attempts=[old])
    assert _snapshot(phone)["records"]["c-0001"]["attempts"] == 1

    _sync(phone, resets={"c-0001": None})
    assert _snapshot(phone)["records"]["c-0001"]["attempts"] == 0

    # 客户端重发那条旧流水：它已在库里，落到 DO NOTHING，不该再累加
    result = _sync(phone, attempts=[old])
    assert result["attemptsAccepted"] == 0
    assert _snapshot(phone)["records"]["c-0001"]["attempts"] == 0


def test_reset_can_set_imported_baseline(devices) -> None:  # noqa: ANN001
    """导入备份：直接设基线（这些计数没有对应流水）。"""
    phone = devices["phone"]
    _sync(
        phone,
        resets={"c-0001": {"attempts": 7, "correct": 5, "wrong": 2, "firstAt": T0, "lastAt": T0 + 9}},
    )

    rec = _snapshot(phone)["records"]["c-0001"]
    assert (rec["attempts"], rec["correct"], rec["wrong"]) == (7, 5, 2)

    # 之后再答一题是「基线 + 1」，不是从 0 开始
    _sync(phone, attempts=[_attempt("c-0001", at=T0 + 100)])
    assert _snapshot(phone)["records"]["c-0001"]["attempts"] == 8


# ------------------------------------------------------------------ 脏数据


def test_invalid_attempts_are_dropped_not_fatal(devices) -> None:  # noqa: ANN001
    """一条脏流水不该让整批作答都存不上。"""
    phone, topic = devices["phone"], devices["topic"]
    result = _sync(
        phone,
        attempts=[
            {"id": "not-a-uuid", "questionId": "c-0001", "at": T0, "status": "correct"},
            {"id": str(uuid.uuid4()), "questionId": "", "at": T0, "status": "correct"},
            {"id": str(uuid.uuid4()), "questionId": "c-0001", "at": T0, "status": "bogus"},
            # 时间离谱到 datetime 装不下。不拦住的话 fromtimestamp 抛异常、
            # 整批作答一起 500 —— 这条断言就是防止它再次发生。
            {"id": str(uuid.uuid4()), "questionId": "c-0001", "at": 10**18, "status": "correct"},
            {"id": str(uuid.uuid4()), "questionId": "c-0001", "at": 0, "status": "correct"},
            _attempt("c-0001", at=T0, topic=topic),
        ],
    )

    assert result["attemptsAccepted"] == 1
    assert result["attemptsDropped"] == 5
    assert _snapshot(phone)["records"]["c-0001"]["attempts"] == 1


def test_day_comes_from_client_not_server_timezone(devices) -> None:  # noqa: ANN001
    """归属哪天由客户端上报，不由服务端时区推导。

    服务器跑 UTC 时，用户凌晨作答若靠服务端反推会被算到前一天，
    热力图与「今日已练」就会对不上。
    """
    phone, topic = devices["phone"], devices["topic"]
    # 本地是 9/16，但 at 落在 UTC 的 9/15
    _sync(phone, attempts=[_attempt("c-0001", at=T0, day="2026-09-16", topic=topic)])

    days = _snapshot(phone)["days"]
    assert "2026-09-16" in days
    assert "2026-09-15" not in days


def test_snapshot_rev_lets_client_raise_its_clock(devices) -> None:  # noqa: ANN001
    """快照要回传当前最大 rev，供客户端做 Lamport 抬高。"""
    phone = devices["phone"]
    _sync(phone, patches={"c-0001": {"_rev": 7, "flagged": True}})
    assert _snapshot(phone)["rev"] >= 7


# ------------------------------------------------------------------ 隔离


def test_attempts_are_isolated_between_users(client, devices) -> None:  # noqa: ANN001
    """另一台设备换的是**另一个账号**时，绝不能看到前一个账号的流水聚合。"""
    phone, topic = devices["phone"], devices["topic"]
    _sync(phone, attempts=[_attempt("c-0001", at=T0, topic=topic)])

    other = f"other-{uuid.uuid4().hex[:8]}@example.com"
    client.cookies.clear()
    client.post("/api/auth/register", json={"email": other, "password": PASSWORD})

    assert _snapshot(client)["records"] == {}
    assert _snapshot(client)["days"] == {}
