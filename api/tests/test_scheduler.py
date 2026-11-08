"""排程求解器：硬约束一条不许破，摸棱两可的地方要**说得出来**。

这是"自动排程"唯一会被反复怀疑的一层（"它凭什么把我这小时排成那样"），
所以用例只钉两类事：

1. **硬约束**：不与课表/钉住的块重叠、不排进夜里、不跨饭点、深度任务不过精力门槛、
   课后的冷却不被深度任务吃掉；
2. **可解释与可复现**：排不进去要逐条给原因，同一份输入两次求解逐块相同
   （否则"撤销"与差异高亮都会抖）。

CP-SAT 与纯 Python 后备共用同一份 IR，所以硬约束那几条对两个引擎都跑一遍。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pytest

from app import scheduler, wellbeing
from app.models import Plan, PlanItem
from app.schedule_ir import Constraint, structural
from app.scheduler import FixedBlock, ScheduleInput, Task, solve
from app.wellbeing.base import KIND_CONSTRAINT, Advisory, Advice

TZ = timezone(timedelta(hours=8))
DAY = date(2026, 11, 9)  # 周一
ENGINES = ("cpsat", "greedy")


def _moment(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=TZ)


def _minutes(one) -> int:  # noqa: ANN001
    return int((one.end - one.start).total_seconds() // 60)


def _input(
    tasks: list[Task],
    fixed: list[FixedBlock] | None = None,
    days: int = 2,
    energy: list[float] | None = None,
    constraints: list[Constraint] | None = None,
    options: dict | None = None,
    deep_floor: float = 0.6,
) -> ScheduleInput:
    """一份"今天的身体状态已知"的求解输入。默认精力满格、作息 07:00–23:30。"""
    opts = {"targetBedtime": "23:30", "targetWake": "07:00", **(options or {})}
    slots = list(energy or [0.9] * 48)
    si = ScheduleInput(
        start=DAY,
        days=days,
        tz=TZ,
        tasks=list(tasks),
        fixed=list(fixed or []),
        energy={(DAY + timedelta(days=offset)).isoformat(): list(slots) for offset in range(days)},
        constraints=list(constraints if constraints is not None else structural(opts, "23:30", "07:00")),
        options=opts,
        deep_floor=deep_floor,
    )
    # 与 `build_input` 同一条规矩：课后的那半格深度任务不能碰
    cooldown: set[int] = set()
    for block in si.fixed:
        if block.kind != "class":
            continue
        end = block.end.astimezone(TZ)
        cooldown.add((end.hour * 60 + end.minute) // scheduler.SLOT)
    si.cooldown = cooldown
    return si


def _no_overlap(blocks) -> bool:  # noqa: ANN001
    ordered = sorted(blocks, key=lambda one: one.start)
    return all(left.end <= right.start for left, right in zip(ordered, ordered[1:]))


# ============================================================================ 硬约束


@pytest.mark.parametrize("engine", ENGINES)
def test_blocks_never_overlap_classes_or_each_other(engine):  # noqa: ANN001
    fixed = [FixedBlock(start=_moment(DAY, 9), end=_moment(DAY, 10, 30), kind="class", title="高数")]
    solution = solve(_input([Task(id="t1", title="线代", minutes=180)], fixed=fixed), engine=engine)

    assert solution.blocks, "两天空着十几个小时，不该一块都排不进去"
    for block in solution.blocks:
        for busy in fixed:
            assert block.end <= busy.start or block.start >= busy.end, "排到课上了"
    assert _no_overlap(solution.blocks)


@pytest.mark.parametrize("engine", ENGINES)
def test_nothing_lands_in_the_night_or_the_hour_before_bed(engine):  # noqa: ANN001
    tasks = [Task(id=f"t{index}", title=f"任务{index}", minutes=240) for index in range(4)]
    solution = solve(_input(tasks, days=2), engine=engine)

    assert solution.blocks
    for block in solution.blocks:
        start = block.start.astimezone(TZ)
        end = block.end.astimezone(TZ)
        assert (start.hour, start.minute) >= (7, 30), f"起床半小时内就排了：{start}"
        assert end.date() == start.date(), "一块不能跨过午夜"
        assert (end.hour * 60 + end.minute) <= 22 * 60 + 30, f"睡前一小时还在排：{end}"


@pytest.mark.parametrize("engine", ENGINES)
def test_meals_are_left_alone(engine):  # noqa: ANN001
    tasks = [Task(id=f"t{index}", title=f"任务{index}", minutes=180) for index in range(3)]
    solution = solve(_input(tasks, days=1), engine=engine)

    for block in solution.blocks:
        start = block.start.astimezone(TZ)
        end = block.end.astimezone(TZ)
        for begin, close in ((12 * 60, 12 * 60 + 40), (18 * 60, 18 * 60 + 40)):
            assert not (start.hour * 60 + start.minute < close and end.hour * 60 + end.minute > begin)


@pytest.mark.parametrize("engine", ENGINES)
def test_deep_work_only_starts_where_energy_clears_the_floor(engine):  # noqa: ANN001
    """下午精力掉到 0.2（模拟没睡好）→ 深度任务只能落在上午。"""
    energy = [0.8] * 48
    for slot in range(24, 48):
        energy[slot] = 0.2
    solution = solve(
        _input([Task(id="deep", title="推导", minutes=180, focus="deep")], days=1, energy=energy),
        engine=engine,
    )

    assert solution.blocks
    for block in solution.blocks:
        assert block.start.astimezone(TZ).hour < 12, "深度任务排到了精力不够的时段"


def test_deep_work_waits_out_the_cooldown_after_class():
    """课 08:00–08:30 之后的那半格不排深度任务：下课要喘口气。

    07:30 开门，但 07:30–08:30 与课重叠、08:30–09:00 是冷却格 → 最早只能 09:00。
    """
    fixed = [FixedBlock(start=_moment(DAY, 8), end=_moment(DAY, 8, 30), kind="class")]
    solution = solve(
        _input([Task(id="deep", title="证明", minutes=60, focus="deep")], fixed=fixed, days=1),
        engine="greedy",
    )

    assert solution.blocks
    assert solution.blocks[0].start.astimezone(TZ).time() == time(9, 0)


@pytest.mark.parametrize("engine", ENGINES)
def test_daily_deep_budget_is_a_ceiling_not_a_hint(engine):  # noqa: ANN001
    """恢复期可能只给 60 分钟深度预算：三条深度任务最多排进去一条。"""
    budget = [Constraint(rule="max_deep_minutes", source="test.budget", params={"minutes": 60})]
    tasks = [Task(id=f"d{index}", title=f"深度{index}", minutes=60, focus="deep") for index in range(3)]
    solution = solve(
        _input(tasks, days=2, constraints=structural({}, "23:30", "07:00") + budget),
        engine=engine,
    )

    per_day: dict[date, int] = {}
    for block in solution.blocks:
        assert block.focus == "deep"
        day = block.start.astimezone(TZ).date()
        per_day[day] = per_day.get(day, 0) + _minutes(block)
    assert per_day, "两天各 60 分钟预算，总得排进去点东西"
    assert all(minutes <= 60 for minutes in per_day.values()), per_day
    assert solution.unscheduled, "排不下的深度任务要报出来，不能悄悄消失"


# ============================================================================ 软约束：文理交替


def test_domains_alternate_instead_of_running_together():
    tasks = [
        Task(id="a1", title="读史", minutes=60, domain="art"),
        Task(id="a2", title="读书评", minutes=60, domain="art"),
        Task(id="s1", title="习题", minutes=60, domain="science"),
        Task(id="s2", title="实验记录", minutes=60, domain="science"),
    ]
    solution = solve(_input(tasks, days=1))

    assert len(solution.blocks) == 4
    ordered = sorted(solution.blocks, key=lambda one: one.start)
    runs = [
        (left.title, right.title)
        for left, right in zip(ordered, ordered[1:])
        if left.domain == right.domain and right.start - left.end <= timedelta(minutes=15)
    ]
    assert runs == [], f"同一主题连着排了：{runs}"


def test_chunks_of_one_task_are_gathered_onto_one_day():
    """同一条待办的两块被排到两天 → 收回到同一天（省一次"重新进入状态"）。"""
    si = _input([Task(id="t1", title="读论文", minutes=120)], days=2)
    split = scheduler.Solution(
        blocks=[
            scheduler.PlacedBlock(
                task_id="t1", title="读论文", start=_moment(DAY, 9), end=_moment(DAY, 10),
                focus="medium", domain="other", reason="",
            ),
            scheduler.PlacedBlock(
                task_id="t1", title="读论文", start=_moment(DAY + timedelta(days=1), 9),
                end=_moment(DAY + timedelta(days=1), 10), focus="medium", domain="other", reason="",
            ),
        ],
        unscheduled=[],
        engine="manual",
        stats={},
        constraints=si.constraints,
    )

    out = scheduler._gather_task_days(si, split)
    assert len({one.start.astimezone(TZ).date() for one in out.blocks}) == 1
    assert _no_overlap(out.blocks)


def test_plan_chunks_caps_what_one_round_looks_at():
    """超长任务不会把求解规模撑爆：一轮只看 `天数 × 3` 块，余量单独报出来。"""
    si = _input([], days=2)
    task = Task(id="long", title="写综述", minutes=3000, splittable=True)

    sizes, tail = scheduler.plan_chunks(si, task)

    assert len(sizes) <= si.days * scheduler.MAX_CHUNKS_PER_DAY
    assert sum(sizes) + tail == 3000


def test_a_task_longer_than_the_week_is_scheduled_as_far_as_it_goes():
    """一周都做不完的长任务：**能排几块排几块**，剩下的说清还差多少。

    回归用户 2026-11-08 的话："有的时候一个任务超级长，一周可能都做不完呢"。
    以前是"要么整条排进去、要么整条推给 unscheduled" —— 于是最长的活儿反而
    一块都排不上，而这恰恰是最需要被铺开的那一条。
    """
    big = Task(
        id="long",
        title="写一份综述",
        minutes=1800,
        focus="deep",
        domain="science",
        splittable=True,
    )
    si = _input([big], days=3)

    out = solve(si, engine="greedy")

    assert out.blocks, "长任务至少该铺上几块"
    assert _no_overlap(out.blocks)
    placed = sum(_minutes(one) for one in out.blocks)
    rest = sum(one["minutes"] for one in out.unscheduled)
    assert placed > 0 and rest > 0
    assert placed + rest == 1800, "排上的 + 报出的剩余 = 原计划，不能凭空少掉一截"
    assert "还差" in out.unscheduled[0]["reason"]


# ============================================================================ 确定性


@pytest.mark.parametrize("engine", ENGINES)
def test_the_same_input_solves_to_the_same_blocks(engine):  # noqa: ANN001
    """同一份输入两次求解必须逐块相同 —— 差异高亮与撤销都靠这个前提。"""
    tasks = [
        Task(id="t1", title="线代", minutes=120, domain="science"),
        Task(id="t2", title="论文", minutes=90, domain="art"),
        Task(id="t3", title="口语", minutes=60, domain="skill", focus="light"),
    ]
    first = solve(_input(tasks, days=3), engine=engine)
    second = solve(_input(tasks, days=3), engine=engine)

    assert [one.as_dict() for one in first.blocks] == [one.as_dict() for one in second.blocks]
    assert first.unscheduled == second.unscheduled


# ============================================================================ 排不进去要有理由


def test_an_oversized_unsplittable_task_says_it_is_too_long():
    solution = solve(_input([Task(id="big", title="一整天的大作业", minutes=300, splittable=False)], days=1))

    assert solution.blocks == []
    assert solution.unscheduled
    assert str(scheduler.MAX_CHUNK) in solution.unscheduled[0]["reason"]


def test_a_past_deadline_is_named_as_the_reason():
    solution = solve(_input([Task(id="late", title="迟了", minutes=60, deadline=DAY - timedelta(days=1))]))

    assert solution.blocks == []
    assert "截止" in solution.unscheduled[0]["reason"]


def test_a_hopelessly_full_day_names_the_energy_gate():
    """精力全线不够 → 原因要指向精力门槛，而不是含糊的"没排上"。"""
    solution = solve(
        _input(
            [Task(id="deep", title="推导", minutes=60, focus="deep")],
            days=1,
            energy=[0.1] * 48,
        )
    )

    assert solution.blocks == []
    assert "精力" in solution.unscheduled[0]["reason"]


# ============================================================================ 可插拔：新策略 → IR → 结果


def test_a_new_health_advisory_changes_the_schedule_through_the_ir(client, db_session):  # noqa: ANN001
    """注册一条新建议（不碰求解器），断言 IR 里有它、且排程结果随之改变。

    这是"健康是框架不是公式"落到求解层的判据：以后加 HRV / 生病 / 咖啡因，
    走的都是这条路。
    """
    plan = Plan(title="考试周")
    db_session.add(plan)
    db_session.flush()
    db_session.add(
        PlanItem(
            plan_id=plan.id,
            title="推导训练",
            estimate_min=120,
            focus="deep",
            domain="science",
        )
    )
    db_session.commit()

    before = scheduler.solve(scheduler.build_input(db_session, days=3))
    assert any(block.focus == "deep" for block in before.blocks), "默认策略下深度任务应该排得进去"

    class NoDeepDay(Advisory):
        key = "test_no_deep"
        label = "测试：今天不许深度用脑"

        def advise(self, ctx, signals, curve):  # noqa: ANN001, ANN201
            return [
                Advice(
                    key="test_no_deep.zero",
                    kind=KIND_CONSTRAINT,
                    severity="warn",
                    text="今天一点深度都别做",
                    params={"rule": "max_deep_minutes", "minutes": 0},
                    source=self.key,
                )
            ]

    wellbeing.register_advisory(NoDeepDay())
    try:
        si = scheduler.build_input(db_session, days=3)
        rules = {(one.rule, one.source) for one in si.constraints}
        assert ("max_deep_minutes", "test_no_deep") in rules, "新策略没有变成 IR"

        after = scheduler.solve(si)
        assert not any(block.focus == "deep" for block in after.blocks), "IR 有了，结果却没变"
        assert any(one["taskId"] for one in after.unscheduled)
    finally:
        wellbeing.ADVISORIES.pop("test_no_deep", None)

    restored = scheduler.solve(scheduler.build_input(db_session, days=3))
    assert any(block.focus == "deep" for block in restored.blocks)


def test_build_input_treats_previous_runs_blocks_as_recyclable_and_locked_ones_as_facts(client):  # noqa: ANN001
    """课表 / 锁定 / 手动块是障碍；上一轮求解器排的块这次要**回收**。"""
    start = (datetime.now().astimezone() + timedelta(hours=2)).replace(second=0, microsecond=0)
    created = client.post(
        "/api/schedule",
        json={
            "title": "上一轮排的",
            "startAt": start.astimezone(timezone.utc).isoformat(),
            "endAt": (start + timedelta(minutes=60)).astimezone(timezone.utc).isoformat(),
        },
    )
    assert created.status_code == 200, created.text
    event_id = created.json()["event"]["id"]

    from app.db import get_session_factory
    from app.models import ScheduleEvent

    session = get_session_factory()()
    try:
        row = session.get(ScheduleEvent, event_id)
        row.run_id = "run-previous"
        session.commit()
    finally:
        session.close()

    from app.db import get_session_factory as factory

    session = factory()()
    try:
        si = scheduler.build_input(session, days=2)
    finally:
        session.close()
    assert all(one.title != "上一轮排的" for one in si.fixed), "上一轮的产物被当成了障碍"

    session = factory()()
    try:
        row = session.get(ScheduleEvent, event_id)
        row.run_id = None
        row.locked = True
        session.commit()
        si = scheduler.build_input(session, days=2)
    finally:
        session.close()
    assert any(one.title == "上一轮排的" for one in si.fixed), "钉住的块必须当事实"
