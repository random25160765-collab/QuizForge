"""健康模块：打卡、睡眠债、档位时间线，以及**扩展点真的可插拔**。

用户对这块的要求是硬性的：

> 健康管理也要做抽象策略，绝对不能写死，因为不同时期我的身体机能差异很大，
> 而且我后续还会对这些东西进行补充。

所以最后一条用例会现场注册一个假的指标与假建议（不碰任何核心文件），
断言它们必须出现在 `/api/wellbeing/today` 里 —— 框架是不是真的，只认这个。
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

from app import wellbeing
from app.wellbeing.base import KIND_CHAT, Advisory, Advice, Metric, Signal


def _at(day_offset: int, hour: int, minute: int = 0) -> str:
    """今天往前 `day_offset` 天的本地时刻（ISO，带时区）。"""
    now = datetime.now().astimezone()
    day = now.date() - timedelta(days=day_offset)
    return datetime.combine(day, time(hour, minute), tzinfo=now.tzinfo).isoformat()


def _night(client, bed_offset: int, bed_hour: int, wake_offset: int, wake_hour: int) -> dict:  # noqa: ANN001
    client.post("/api/wellbeing/sleep/start", json={"at": _at(bed_offset, bed_hour)})
    res = client.post("/api/wellbeing/sleep/wake", json={"at": _at(wake_offset, wake_hour)})
    assert res.status_code == 200, res.text
    return res.json()["sleep"]


def test_check_in_opens_and_closes_one_night(client):  # noqa: ANN001
    """「睡了」开一条、「起床了」合上；时长是算出来的，不是填出来的。"""
    started = client.post("/api/wellbeing/sleep/start", json={"at": _at(1, 23, 30)})
    assert started.status_code == 200, started.text
    assert started.json()["sleep"]["wakeAt"] is None
    assert started.json()["today"]["active"] is not None

    woke = client.post("/api/wellbeing/sleep/wake", json={"at": _at(0, 7, 10)}).json()
    assert woke["sleep"]["minutes"] == 460, "23:30 → 07:10 应该是 7 小时 40 分"
    assert woke["today"]["active"] is None
    assert woke["today"]["sleep"]["lastNightMin"] == 460


def test_tapping_sleep_twice_does_not_open_two_sessions(client):  # noqa: ANN001
    """手滑按早了一次，再按一次是**校正时刻**，不是再开一条。"""
    client.post("/api/wellbeing/sleep/start", json={"at": _at(1, 22, 0)})
    client.post("/api/wellbeing/sleep/start", json={"at": _at(1, 23, 30)})

    logs = client.get("/api/wellbeing/sleep?days=7").json()["logs"]
    assert len(logs) == 1
    # 接口回的是 UTC，比较**时刻**而不是字符串
    assert datetime.fromisoformat(logs[0]["bedAt"]) == datetime.fromisoformat(_at(1, 23, 30))


def test_less_sleep_means_lower_energy_and_a_smaller_deep_budget(client):  # noqa: ANN001
    """睡得少 → 精力低、深度预算小。这是整个健康模块唯一必须成立的单调性。"""
    _night(client, 1, 23, 0, 7)
    rested = client.get("/api/wellbeing/today").json()

    # 再造三晚"三点睡、六点起"（当晚 3h，别写成跨 27 小时）
    for offset in (2, 3, 4):
        _night(client, offset, 3, offset, 6)

    tired = client.get("/api/wellbeing/today").json()
    assert tired["sleep"]["debtHours"] > rested["sleep"]["debtHours"]
    assert tired["curve"]["deepBudgetMin"] < rested["curve"]["deepBudgetMin"]

    rested_energy = sum(rested["curve"]["slots"][16:44]) / 28
    tired_energy = sum(tired["curve"]["slots"][16:44]) / 28
    assert tired_energy < rested_energy

    # 欠觉够多时会多出一条"总量压一压"的约束（不是靠界面提示，而是约束本身）
    nudges = [one for one in tired["advice"] if one["key"].startswith("sleep_debt_guard")]
    assert nudges, "欠了十几小时还没有任何建议？"


def test_a_regime_changes_the_profile_and_its_advice(client):  # noqa: ANN001
    """身体状态按日期区间换档：今天在恢复期里，就不该按标准档排。"""
    _night(client, 1, 23, 0, 7)
    standard = client.get("/api/wellbeing/today").json()

    created = client.post(
        "/api/wellbeing/regimes",
        json={
            "title": "病后恢复",
            "startDate": standard["today"],
            "profile": "recovery",
            "note": "这两周只做轻的",
        },
    )
    assert created.status_code == 200, created.text
    assert created.json()["today"]["profile"] == "recovery"

    today = created.json()["today"]
    assert today["curve"]["deepBudgetMin"] < standard["curve"]["deepBudgetMin"]
    keys = {one["key"] for one in today["advice"]}
    assert "recovery_day.no-deep" in keys

    # 档位是时间线：明天的档位不该被这条覆盖（它是无限期的，所以要删掉才算数）
    regime_id = created.json()["regime"]["id"]
    assert client.delete(f"/api/wellbeing/regimes/{regime_id}").status_code == 200
    assert client.get("/api/wellbeing/today").json()["profile"] == "standard"


def test_patch_and_delete_a_sleep_log(client):  # noqa: ANN001
    row = _night(client, 1, 23, 0, 7)
    fixed = client.patch(
        f"/api/wellbeing/sleep/{row['id']}",
        json={"bedAt": _at(1, 22, 30), "quality": 4, "note": "睡得还行"},
    )
    assert fixed.status_code == 200, fixed.text
    assert fixed.json()["sleep"]["minutes"] == 510
    assert fixed.json()["sleep"]["quality"] == 4

    bad = client.patch(f"/api/wellbeing/sleep/{row['id']}", json={"wakeAt": _at(1, 21, 0)})
    assert bad.status_code == 400, "醒来早于入睡必须被拒"

    assert client.delete(f"/api/wellbeing/sleep/{row['id']}").status_code == 200
    assert client.get("/api/wellbeing/sleep?days=7").json()["logs"] == []


def test_strategies_endpoint_lists_the_registry(client):  # noqa: ANN001
    """设置页的下拉直接读注册表：加了策略就自动出现，不用改前端。"""
    body = client.get("/api/wellbeing/strategies").json()
    assert {"standard", "recovery"} <= {one["key"] for one in body["profiles"]}
    assert "sleep_debt" in {one["key"] for one in body["metrics"]}
    assert body["options"]["targetBedtime"] == "23:30"


def test_new_metric_and_advisory_plug_in_without_touching_core(client, db_session):  # noqa: ANN001
    """扩展点的判据：现场注册，接口里就得有。

    这段代码**不 import 任何核心实现**，只碰公开的 `register_*` 与协议基类 ——
    以后加 HRV、加"病了就只回一句去休息"，走的就是这条路。
    """

    class MoodMetric(Metric):
        key = "test_mood"
        label = "心情（测试）"

        def compute(self, ctx):  # noqa: ANN001, ANN201
            return Signal(
                key=self.key,
                label=self.label,
                value=0.25,
                raw={"mood": "低落"},
                note="今天心情一般",
            )

    class RestAdvice(Advisory):
        key = "test_rest"
        label = "测试建议"

        def advise(self, ctx, signals, curve):  # noqa: ANN001, ANN201
            assert signals["test_mood"].value == 0.25
            return [
                Advice(
                    key="test_rest.chat",
                    kind=KIND_CHAT,
                    severity="warn",
                    text="今天别逼自己",
                    params={"rule": "max_minutes", "minutes": 60},
                    source=self.key,
                )
            ]

    wellbeing.register_metric(MoodMetric())
    wellbeing.register_advisory(RestAdvice())
    try:
        body = client.get("/api/wellbeing/today").json()
        assert "test_mood" in {one["key"] for one in body["signals"]}
        assert "test_rest.chat" in {one["key"] for one in body["advice"]}
        # 对话侧只读 chat_intervention —— 新建议无需改对话代码就能进去
        assert "今天别逼自己" in wellbeing.chat_lines(db_session)
    finally:
        wellbeing.METRICS.pop("test_mood", None)
        wellbeing.ADVISORIES.pop("test_rest", None)

    after = client.get("/api/wellbeing/today").json()
    assert "test_mood" not in {one["key"] for one in after["signals"]}


def test_only_hard_advice_asks_the_model_to_stop_the_topic():
    """催睡是 `chat_intervention` 里的一条；"压过话题"只属于 `severity == hard`。

    对话侧不许自己判断"现在几点了"，只认这条建议 —— 以后加"病了就别学了"同理。
    """
    from app.routers.chat import build_prompt, wellbeing_notice
    from app.wellbeing.base import Advice

    assert wellbeing_notice([]) == ""
    assert wellbeing_notice([Advice(key="s", kind="schedule_constraint", severity="hard", text="排轻点")]) == ""

    soft = wellbeing_notice([Advice(key="soft", kind=KIND_CHAT, severity="warn", text="今天早点收")])
    assert "今天早点收" in soft
    assert "压过话题" not in soft, "一般提醒不该让它闭嘴"

    hard = wellbeing_notice([Advice(key="hard", kind=KIND_CHAT, severity="hard", text="现在该睡了")])
    assert "现在该睡了" in hard
    assert "只回一句" in hard and "压过话题" in hard

    # 两种模式都要带：极简模式只是没有工具，不是看不见他该睡了
    assert "现在该睡了" in build_prompt({}, health=hard)
    assert "现在该睡了" in build_prompt({"library": ()}, dashboard="（他的进度）", health=hard)
