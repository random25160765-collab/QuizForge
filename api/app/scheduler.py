"""自动排程：把待办按精力曲线塞进日历。

## 三层的分工

    wellbeing   →  精力曲线 + 建议（不知道求解器）
    schedule_ir →  约束 IR（两边都只认这份词汇表）
    scheduler   →  这里：把 IR 翻成 CP-SAT（或纯 Python 后备），出块与解释

**这个文件不知道"睡眠债"是什么**，也不知道"恢复期"。它只认 `Constraint`：
`no_window` / `max_deep_minutes` / `energy_floor` / `max_minutes` / `cooldown_after`，
软约束进目标函数。所以加一种健康策略不用改这里。

## 为什么用 30 分钟网格

块长取 30 的倍数（30 / 60 / 90），起点对齐到整点或半点：
* 日历本身就是半小时刻度，25 分钟的块会留 5 分钟缝、渲染还要处理半格；
* 网格化之后"占格"是线性的（每个 30 分钟格最多一个块），CP-SAT 不需要区间变量，
  几十个任务 × 几百个候选点仍然秒解。

## 确定性

任务按 `(截止日, id)`、候选按时间升序生成；CP-SAT 单 worker、固定种子。
同一份输入两次求解**逐块相同** —— 否则"撤销"与"差异高亮"都会抖。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date as date_type, datetime, time, timedelta, tzinfo
from typing import Iterable

from sqlalchemy import select

from . import schedule_ir as ir
from . import wellbeing
from .models import PlanItem, ScheduleEvent
from .schedule_ir import Constraint

SLOT = 30  # 分钟
SLOTS_PER_DAY = 48
#: 一天最多的学习时长（硬上限，和健康的 `max_minutes` 取更紧的那个）
DEFAULT_TOTAL_CAP = 480
#: 单个主题每天最多多久（"不可以学一个主题太久"）。**软**约束：超了扣分。
DEFAULT_DOMAIN_CAP = 180
#: 超出一分钟的扣分。一块的"排得下"奖励是 1000，所以这个权重只影响"怎么排"，
#: 不会把本来排得下的块挤出去。
DEFAULT_CAP_WEIGHT = 2.0
#: 一节课之后多少分钟不排深度任务
DEFAULT_COOLDOWN = 30


# ============================================================================ 输入


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    minutes: int
    focus: str = "medium"
    domain: str = "other"
    splittable: bool = True
    max_chunk_min: int = 0
    earliest: date_type | None = None
    deadline: date_type | None = None
    important: bool = False
    points: tuple[str, ...] = ()

    @property
    def deep(self) -> bool:
        return self.focus == "deep"


@dataclass(frozen=True)
class FixedBlock:
    start: datetime
    end: datetime
    kind: str = "manual"  # class / manual / locked
    title: str = ""


@dataclass
class ScheduleInput:
    start: date_type
    days: int
    tz: tzinfo
    tasks: list[Task]
    fixed: list[FixedBlock]
    #: 每天 48 个槽的精力（key 是 `YYYY-MM-DD`）
    energy: dict[str, list[float]]
    constraints: list[Constraint] = field(default_factory=list)
    options: dict = field(default_factory=dict)
    deep_floor: float = 0.6
    #: 课后的冷却格（深度任务不能碰）
    cooldown: set[int] = field(default_factory=set)
    #: 这次求解看到的是哪套精力策略 / 哪一档身体状态（写进 `schedule_runs`，事后可复现）
    profile: str = ""
    regime_id: str | None = None
    #: 上一轮排过的位置（taskId → 起点槽位）。只是**软**偏好：同样的排法能保住就不动，
    #: 否则每加一条待办，整周都跟着重排 —— 界面上"移动 14 块"比排得完美更让人不敢点。
    previous: dict[str, set[int]] = field(default_factory=dict)


@dataclass(frozen=True)
class PlacedBlock:
    task_id: str
    title: str
    start: datetime
    end: datetime
    focus: str
    domain: str
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "taskId": self.task_id,
            "title": self.title,
            "start": self.start.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "end": self.end.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "focus": self.focus,
            "domain": self.domain,
            "reason": self.reason,
        }


@dataclass
class Solution:
    blocks: list[PlacedBlock]
    unscheduled: list[dict]
    engine: str
    stats: dict
    constraints: list[Constraint] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "blocks": [one.as_dict() for one in self.blocks],
            "unscheduled": list(self.unscheduled),
            "engine": self.engine,
            "stats": dict(self.stats),
            "constraints": [one.as_dict() for one in self.constraints],
        }


# ============================================================================ 组装输入


def recyclable(row: ScheduleEvent) -> bool:
    """这一块能不能被重排**回收**。

    只有"上一轮求解器排的、用户没动过、也不是订阅来的"才回收。三条例外各有理由：
    `locked` 是用户钉住的；`detached` 是用户拖过/改过的（订阅同步都会跳过它，
    重排更没有资格抹掉）；`sub_id` 是课表——那不是我们排的。
    """
    return bool(row.run_id) and not row.locked and not row.detached and not row.sub_id


def _hm(value: time) -> str:
    return f"{value.hour:02d}:{value.minute:02d}"


def _clock(raw: str | None, fallback: time) -> time:
    try:
        hour, minute = str(raw or "").split(":", 1)
        return time(int(hour), int(minute))
    except (TypeError, ValueError):
        return fallback


def _local_midnight(day: date_type, tz: tzinfo) -> datetime:
    return datetime.combine(day, time(0, 0), tzinfo=tz)


def _as_local(moment: datetime, tz: tzinfo) -> datetime:
    return moment.astimezone(tz) if moment.tzinfo else moment.replace(tzinfo=UTC).astimezone(tz)


def _cell(si: ScheduleInput, moment: datetime) -> int:
    local = _as_local(moment, si.tz)
    offset = (local.date() - si.start).days
    return offset * SLOTS_PER_DAY + (local.hour * 60 + local.minute) // SLOT


def _slot_start(si: ScheduleInput, index: int) -> datetime:
    day = si.start + timedelta(days=index // SLOTS_PER_DAY)
    minutes = (index % SLOTS_PER_DAY) * SLOT
    return _local_midnight(day, si.tz) + timedelta(minutes=minutes)


def _energy_at(si: ScheduleInput, moment: datetime) -> float:
    local = _as_local(moment, si.tz)
    slots = si.energy.get(local.date().isoformat())
    if not slots:
        return 0.6
    return float(slots[(local.hour * 60 + local.minute) // SLOT])


def build_input(db, days: int = 7, now: datetime | None = None) -> ScheduleInput:  # noqa: ANN001
    """从库里攒出一次求解所需的全部输入。**只读**，不写任何东西。"""
    now = now or wellbeing.local_now()
    days = max(1, min(14, int(days)))
    ctx, _signals, curve, advice = wellbeing.evaluate(db, now)

    options = dict(ctx.options)
    constraints = ir.collect(advice, options, _hm(ctx.target_bed), _hm(ctx.target_wake))
    # 门禁没过的条目要往前站（`priority_boost` 是软约束，只影响先后，不保证排得进）
    from . import gates  # noqa: PLC0415 —— 避免模块级循环导入

    constraints += gates.priority_constraints(db)

    # 精力：今天用真实曲线；后面几天沿用同一条形状（假设作息相近）。
    # 不预测未来的睡眠 —— 那是"猜"，而排程要的是"按今天的状态铺开"。
    energy = {
        (now.date() + timedelta(days=offset)).isoformat(): list(curve.slots)
        for offset in range(days)
    }

    horizon_start = _local_midnight(now.date(), now.tzinfo)
    horizon_end = horizon_start + timedelta(days=days)
    rows = db.scalars(
        select(ScheduleEvent).where(
            ScheduleEvent.start_at < horizon_end.astimezone(UTC),
            ScheduleEvent.end_at > horizon_start.astimezone(UTC),
        )
    ).all()

    fixed: list[FixedBlock] = []
    cooldown: set[int] = set()
    for row in rows:
        if not recyclable(row):
            fixed.append(
                FixedBlock(
                    start=row.start_at,
                    end=row.end_at,
                    kind="class" if row.sub_id else "manual",
                    title=row.title,
                )
            )

    si = ScheduleInput(
        start=now.date(),
        days=days,
        tz=now.tzinfo,
        tasks=[],
        fixed=fixed,
        energy=energy,
        constraints=constraints,
        options=options,
        deep_floor=float(ir.hard_value(constraints, "energy_floor", 0.6) or 0.6),
        profile=ctx.profile_key,
        regime_id=(ctx.regime or {}).get("id"),
    )

    # 课后的冷却格：深度任务在这 30 分钟里不开始、也不跨进去
    for block in fixed:
        if block.kind != "class":
            continue
        start = _cell(si, block.end)
        for index in range(start, min(si.days * SLOTS_PER_DAY, start + 1)):
            cooldown.add(index)
    si.cooldown = cooldown

    items = db.scalars(select(PlanItem).where(PlanItem.done.is_(False))).all()
    si.tasks = [
        Task(
            id=row.id,
            title=row.title,
            minutes=max(30, int(row.estimate_min or 60)),
            focus=row.focus or "medium",
            domain=row.domain or "other",
            splittable=bool(row.splittable),
            max_chunk_min=int(row.max_chunk_min or 0),
            earliest=row.earliest_date,
            deadline=row.due_date,
            important=bool(row.important),
            points=tuple(row.points or []),
        )
        for row in items
    ]
    # 上一轮排在哪：既是"别乱挪"的软偏好，也是排序依据（老任务在前、新任务补空档）
    begin = datetime.combine(si.start, time(0, 0), tzinfo=si.tz)
    previous: dict[str, set[int]] = defaultdict(set)
    for row in rows:
        if not recyclable(row) or not row.item_id:
            continue
        index = int((row.start_at - begin) // timedelta(minutes=SLOT))
        if 0 <= index < si.days * SLOTS_PER_DAY:
            previous[row.item_id].add(index)
    si.previous = {key: value for key, value in previous.items()}

    si.tasks.sort(
        key=lambda one: (
            min(si.previous[one.id]) if one.id in si.previous else si.days * SLOTS_PER_DAY,
            one.deadline or date_type.max,
            one.id,
        )
    )
    return si


# ============================================================================ 块长


#: 一块的绝对上限。超过它**不排**（不是排成一整块）：日历上一连坐五小时
#: 既不可执行、也让"精力上限"形同虚设。用户在计划里写 300 分钟要自己拆。
MAX_CHUNK = 240


#: 一条待办一天最多排几块。它防的不是"排太多"，是**求解规模**：
#: 一个 100 小时的活按 90 分钟切会得到 67 块，变量数会随块数线性长；
#: 而一天真能坐下来的深度时段也就三块。超出上限的部分进 `unscheduled`（说清还剩多少）。
MAX_CHUNKS_PER_DAY = 3


def chunks_of(task: Task) -> list[int]:
    """把预计时长切成若干个 30 分钟倍数的块。

    * 不可拆的整块排；超过 `MAX_CHUNK` 就返回空（进 `unscheduled` 并说明）；
    * 可拆的按"每块不超过 `max_chunk_min`（默认 90）"均分。
    """
    minutes = max(30, int(task.minutes or 60))
    if not task.splittable:
        return [int(round(minutes / SLOT)) * SLOT] if minutes <= MAX_CHUNK else []

    max_chunk = int(task.max_chunk_min or 90)
    max_chunk = max(30, min(240, max_chunk))
    units_total = max(1, int(round(minutes / SLOT)))
    count = max(1, -(-units_total // (max_chunk // SLOT)))
    base, extra = divmod(units_total, count)
    sizes = []
    for index in range(count):
        units = base + (1 if index < extra else 0)
        sizes.append(max(1, min(max_chunk // SLOT, units)) * SLOT)
    return [size for size in sizes if size > 0]


def plan_chunks(si: ScheduleInput, task: Task) -> tuple[list[int], int]:
    """这条待办这次**考虑**切成哪几块，剩下多少分钟这一轮不碰。

    第二项不是"排不下"，是"这一轮不打算排这么多"（见 `MAX_CHUNKS_PER_DAY`）。
    两者在界面上都要说清：用户看到"还差 12h"才知道这活是跨周的，而不是坏了。
    """
    sizes = chunks_of(task)
    limit = max(1, si.days) * MAX_CHUNKS_PER_DAY
    if len(sizes) <= limit:
        return sizes, 0
    return sizes[:limit], sum(sizes[limit:])


def _span_text(minutes: int) -> str:
    m = max(0, int(minutes))
    if m < 60:
        return f"{m}m"
    return f"{m // 60}h{m % 60:02d}m" if m % 60 else f"{m // 60}h"


# ============================================================================ 候选点与网格


def _windows(si: ScheduleInput, day: date_type) -> list[tuple[datetime, datetime]]:
    """当天被 `no_window` 挡掉的时段（睡眠、三餐）。"""
    bedtime = _clock(si.options.get("targetBedtime"), time(23, 30))
    waketime = _clock(si.options.get("targetWake"), time(7, 0))
    out: list[tuple[datetime, datetime]] = []
    for one in si.constraints:
        if one.rule != "no_window":
            continue
        start = _clock(one.params.get("start"), bedtime)
        end = _clock(one.params.get("end"), waketime)
        first = _local_midnight(day, si.tz).replace(hour=start.hour, minute=start.minute)
        if end <= start:
            last = _local_midnight(day + timedelta(days=1), si.tz).replace(
                hour=end.hour, minute=end.minute
            )
        else:
            last = first.replace(hour=end.hour, minute=end.minute)
        out.append((first, last))
    return out


def _day_open_close(si: ScheduleInput, day: date_type) -> tuple[datetime, datetime]:
    """可排的时间范围：起床半小时之后 到 睡觉前一小时。"""
    wake = _clock(si.options.get("targetWake"), time(7, 0))
    bed = _clock(si.options.get("targetBedtime"), time(23, 30))
    open_at = _local_midnight(day, si.tz) + timedelta(
        hours=wake.hour, minutes=wake.minute + 30
    )
    close_at = _local_midnight(day, si.tz) + timedelta(hours=bed.hour, minutes=bed.minute - 60)
    if close_at <= open_at:
        close_at = open_at + timedelta(hours=12)
    return open_at, close_at


def _blocked_cells(si: ScheduleInput) -> list[bool]:
    cells = [False] * (si.days * SLOTS_PER_DAY)
    for block in si.fixed:
        first = _cell(si, block.start)
        last = max(first + 1, _cell(si, block.end - timedelta(seconds=1)) + 1)
        for index in range(max(0, first), min(len(cells), last)):
            cells[index] = True
    return cells


def _overlaps_blocked(blocked: list[bool], start: int, size: int) -> bool:
    cells = max(1, size // SLOT)
    if start + cells > len(blocked):
        return True
    return any(blocked[index] for index in range(start, start + cells))


def _candidates(
    si: ScheduleInput, task: Task, size: int, blocked: list[bool]
) -> list[int]:
    """这个任务的一个块可以放在哪些格子起点上（按时间升序、确定性）。"""
    out: list[int] = []
    cells = max(1, size // SLOT)
    for offset in range(si.days):
        day = si.start + timedelta(days=offset)
        if task.earliest and day < task.earliest:
            continue
        if task.deadline and day > task.deadline:
            continue
        open_at, close_at = _day_open_close(si, day)
        windows = _windows(si, day)
        for slot in range(SLOTS_PER_DAY):
            start = _local_midnight(day, si.tz) + timedelta(minutes=slot * SLOT)
            end = start + timedelta(minutes=size)
            if start < open_at or end > close_at:
                continue
            if any(start < right and end > left for left, right in windows):
                continue
            index = offset * SLOTS_PER_DAY + slot
            if _overlaps_blocked(blocked, index, size):
                continue
            if task.deep:
                if _energy_at(si, start) < si.deep_floor:
                    continue
                if any(index + step in si.cooldown for step in range(cells)):
                    continue
            out.append(index)
    return out


# ============================================================================ 求解


def solve(si: ScheduleInput, engine: str = "auto") -> Solution:
    """求解。`engine` = auto / cpsat / greedy（测后备实现时用 greedy）。"""
    grids = _blocked_cells(si)
    if engine in ("auto", "cpsat"):
        try:
            solution = _solve_cpsat(si, grids)
            if solution is not None and _validate(si, solution.blocks):
                solution = _gather_task_days(si, solution)
                solution = _break_domain_runs(si, solution)
                return solution
        except ImportError:
            pass  # 没装 ortools：落到后备
        except Exception:  # noqa: BLE001 —— 求解器内部的任何异常都不该让重排失败
            pass
    solution = _solve_greedy(si, grids)
    solution = _gather_task_days(si, solution)
    solution = _break_domain_runs(si, solution)
    return solution


def _explain(si: ScheduleInput, task: Task, start: datetime) -> str:
    local = _as_local(start, si.tz)
    moment = "上午" if local.hour < 12 else "下午" if local.hour < 18 else "晚上"
    bits = [f"{moment} · 精力 {_energy_at(si, start):.2f}"]
    if task.deep:
        bits.append(f"深度任务（门槛 {si.deep_floor:g}）")
    if task.deadline:
        left = (task.deadline - local.date()).days
        bits.append("今天截止" if left <= 0 else f"距截止 {left} 天")
    if task.domain != "other":
        bits.append({"art": "文科", "science": "理科", "skill": "技能"}.get(task.domain, task.domain))
    return " · ".join(bits)


def explain_unscheduled(si: ScheduleInput, task: Task, blocked: list[bool]) -> str:
    """为什么这条排不进去 —— 界面上要逐条说清，不能只给个"没排上"。

    判据全部可查证（截止日、开始日、块长、候选点），不猜"大概时间不够"。
    `blocked` 用**排程前**的网格：那里挡着的是课表与钉住的块，与本次产出无关。
    """
    last_day = si.start + timedelta(days=si.days - 1)
    if task.deadline and task.deadline < si.start:
        return "截止日已过"
    if task.earliest and task.earliest > last_day:
        return "开始日超出售程范围"
    sizes = chunks_of(task)
    if not sizes:
        return f"整块 {max(30, int(task.minutes or 60))} 分钟，超过单块上限 {MAX_CHUNK} 分钟"
    if any(_candidates(si, task, size, blocked) for size in sizes):
        return "能放的位置不够（其余块已经占掉了）"
    if task.deep:
        return f"没有又空、精力又过 {si.deep_floor:g} 的时段（深度任务有精力门槛）"
    return "排程范围内没有这么长的连续空档"


def _remainder_reason(si: ScheduleInput, task: Task, remaining: int, blocked: list[bool]) -> str:
    """排不进去的原因：**一块都没排上**与**只排了一部分**是两件事。

    前者要解释"为什么一条都没有"（截止过了 / 块太长 / 没有够精力的空档）；
    后者要说清还剩多少 —— 长任务排到下周是正常的，不该看着像失败。
    """
    total = max(1, int(task.minutes or 0))
    if remaining >= total:
        return explain_unscheduled(si, task, blocked)
    return (
        f"这周排下 {_span_text(total - remaining)}，还差 {_span_text(remaining)}"
        f"（下次重排接着排）"
    )


def _solution_from_blocks(
    si: ScheduleInput,
    placed: list[tuple[Task, int, int]],
    engine: str,
    skipped: list[tuple[Task, int]],
    blocked: list[bool],
) -> Solution:
    blocks = [
        PlacedBlock(
            task_id=task.id,
            title=task.title,
            start=_slot_start(si, index),
            end=_slot_start(si, index) + timedelta(minutes=size),
            focus=task.focus,
            domain=task.domain,
            reason=_explain(si, task, _slot_start(si, index)),
        )
        for task, index, size in placed
    ]
    blocks.sort(key=lambda one: (one.start, one.task_id))
    minutes = sum(int((one.end - one.start).total_seconds() // 60) for one in blocks)
    deep = sum(
        int((one.end - one.start).total_seconds() // 60) for one in blocks if one.focus == "deep"
    )
    return Solution(
        blocks=blocks,
        unscheduled=[
            {
                "taskId": task.id,
                "title": task.title,
                # 这里是**还剩多少分钟**（不是原计划时长）：长任务排了一部分之后，
                # 界面要显示的是"还差 12h"，与"整条没排上"区分开。
                "minutes": remaining,
                "reason": _remainder_reason(si, task, remaining, blocked),
            }
            for task, remaining in skipped
        ],
        engine=engine,
        stats={"blocks": len(blocks), "minutes": minutes, "deepMinutes": deep},
        constraints=si.constraints,
    )


def _soft_bonus(si: ScheduleInput, task: Task, index: int) -> float:
    """软约束给某个起点的加分。**两个引擎共用同一份算法** —— 否则"解释"与"结果"两套。"""
    bonus = 0.0
    hour = (index % SLOTS_PER_DAY) * SLOT / 60
    day = si.start + timedelta(days=index // SLOTS_PER_DAY)
    for one in si.constraints:
        if one.hard:
            continue
        if one.rule == "prefer_window":
            left = _clock(one.params.get("start"), time(0, 0))
            right = _clock(one.params.get("end"), time(23, 59))
            if left.hour + left.minute / 60 <= hour < right.hour + right.minute / 60:
                bonus += float(one.params.get("bonus") or one.weight or 0)
        elif one.rule == "priority_boost" and one.params.get("taskId") == task.id:
            bonus += float(one.weight or 0)
        elif one.rule == "prefer_early_for_deadline" and task.deadline is not None:
            left_days = (task.deadline - day).days
            if left_days <= 2:
                bonus += float(one.weight or 10) * (3 - max(0, left_days)) / 3
    if task.important:
        bonus += 25
    # 没有显式 `prefer_window` 时的默认：深度任务尽量上午（用户设置里的 deepPref）
    if task.deep and str(si.options.get("deepPref") or "morning") in ("morning", "both") and hour < 12:
        bonus += 12
    # 保守：上一轮就在这个位置就别挪（重排是用来"补新东西"的，不是用来洗牌的）
    if index in si.previous.get(task.id, ()):  # type: ignore[arg-type]
        bonus += 40
    return bonus


def _cap_weight(si: ScheduleInput) -> float:
    """「同一主题单日上限」在目标函数里的权重（IR 给了就用 IR 的）。"""
    for one in si.constraints:
        if one.rule == "cap_topic_minutes" and not one.hard:
            return float(one.weight or DEFAULT_CAP_WEIGHT)
    return DEFAULT_CAP_WEIGHT


def _gather_task_days(si: ScheduleInput, solution: Solution) -> Solution:
    """同一任务的分块尽量并在同一天。

    碎片化不是时长问题，是**"每天都要重新进入状态"**：90 分钟拆成今天 45 + 明天 45，
    实际代价远高于连着 90 分钟。求解器里要对"同一任务的两块是否同一天"建模需要
    成对变量（规模会炸），所以放在求解之后做一次确定性的局部改进 —— 与"文理交替"
    同一层、两个引擎共享。

    只在**同一天还有位置**时才挪；挪不动就保持原样（宁可碎，也不能破硬约束）。
    """
    by_task: dict[str, list] = defaultdict(list)
    for one in solution.blocks:
        by_task[one.task_id].append(one)
    if all(len(ones) < 2 for ones in by_task.values()):
        return solution

    def cells(size: int) -> int:
        return max(1, size // SLOT)

    blocks = sorted(solution.blocks, key=lambda one: (one.start, one.task_id))
    blocked = _blocked_cells(si)
    for one in blocks:
        first = _cell(si, one.start)
        for step in range(cells(int((one.end - one.start).total_seconds() // 60))):
            if 0 <= first + step < len(blocked):
                blocked[first + step] = True

    for task_id in sorted(by_task):
        ones = by_task[task_id]
        if len(ones) < 2:
            continue
        task = next((one for one in si.tasks if one.id == task_id), None)
        if task is None or not task.splittable:
            continue
        days = sorted({_as_local(one.start, si.tz).date() for one in ones})
        if len(days) < 2:
            continue
        home = days[0]
        for current in sorted(ones, key=lambda one: one.start)[1:]:
            if _as_local(current.start, si.tz).date() == home:
                continue
            size = int((current.end - current.start).total_seconds() // 60)
            old = _cell(si, current.start)
            for step in range(cells(size)):
                if 0 <= old + step < len(blocked):
                    blocked[old + step] = False
            target = next(
                (
                    index
                    for index in _candidates(si, task, size, blocked)
                    if si.start + timedelta(days=index // SLOTS_PER_DAY) == home
                ),
                None,
            )
            if target is None:
                for step in range(cells(size)):
                    if 0 <= old + step < len(blocked):
                        blocked[old + step] = True
                continue
            for step in range(cells(size)):
                if 0 <= target + step < len(blocked):
                    blocked[target + step] = True
            moved = PlacedBlock(
                task_id=current.task_id,
                title=current.title,
                start=_slot_start(si, target),
                end=_slot_start(si, target) + timedelta(minutes=size),
                focus=current.focus,
                domain=current.domain,
                reason=_explain(si, task, _slot_start(si, target)),
            )
            blocks[blocks.index(current)] = moved

    solution.blocks = sorted(blocks, key=lambda one: (one.start, one.task_id))
    return solution


def _solve_cpsat(si: ScheduleInput, blocked: list[bool]) -> Solution | None:
    from ortools.sat.python import cp_model  # noqa: PLC0415 —— 可选依赖，导入失败就切后备

    model = cp_model.CpModel()
    placed_vars: dict[tuple[int, int, int], object] = {}
    sizes: list[list[int]] = []
    for task_index, task in enumerate(si.tasks):
        task_sizes, _tail = plan_chunks(si, task)
        sizes.append(task_sizes)
        if not task_sizes:
            continue
        for chunk_index, size in enumerate(task_sizes):
            for start in _candidates(si, task, size, blocked):
                placed_vars[(task_index, chunk_index, start)] = model.NewBoolVar(
                    f"x_{task_index}_{chunk_index}_{start}"
                )

    if not placed_vars:
        return None

    # 同一块只能有一个起点
    by_chunk: dict[tuple[int, int], list] = defaultdict(list)
    for (task_index, chunk_index, _start), var in placed_vars.items():
        by_chunk[(task_index, chunk_index)].append(var)
    for variables in by_chunk.values():
        model.Add(sum(variables) <= 1)

    # 每个 30 分钟格最多一个块
    occupancy: dict[int, list] = defaultdict(list)
    for (task_index, chunk_index, start), var in placed_vars.items():
        size = sizes[task_index][chunk_index]
        for step in range(max(1, size // SLOT)):
            occupancy[start + step].append(var)
    for variables in occupancy.values():
        model.Add(sum(variables) <= 1)

    deep_cap = int(ir.hard_value(si.constraints, "max_deep_minutes", 240) or 0)
    total_cap = int(ir.hard_value(si.constraints, "max_minutes", DEFAULT_TOTAL_CAP) or DEFAULT_TOTAL_CAP)
    domain_cap = int(si.options.get("maxDomainMinPerDay") or DEFAULT_DOMAIN_CAP)
    cap_weight = _cap_weight(si)

    # 硬约束：每日总时长 / 每日深度时长
    penalty_terms = []
    for offset in range(si.days):
        first = offset * SLOTS_PER_DAY
        last = first + SLOTS_PER_DAY
        deep_terms = []
        total_terms = []
        domain_terms: dict[str, list] = defaultdict(list)
        for (task_index, chunk_index, start), var in placed_vars.items():
            if not (first <= start < last):
                continue
            task = si.tasks[task_index]
            size = sizes[task_index][chunk_index]
            total_terms.append(var * size)
            if task.deep:
                deep_terms.append(var * size)
            domain_terms[task.domain].append(var * size)
        if deep_terms:
            model.Add(sum(deep_terms) <= max(0, deep_cap))
        if total_terms:
            model.Add(sum(total_terms) <= max(60, total_cap))
        # 单日单主题上限是**软**约束：超了扣分，而不是禁止（否则"排不进去"会莫名其妙）。
        # `other` 不算主题 —— 把一堆没归类的待办当成同一个主题是最常见的误判。
        for domain, terms in domain_terms.items():
            if domain == "other":
                continue
            excess = model.NewIntVar(0, max(0, total_cap), f"over_{offset}_{domain}")
            model.Add(excess >= sum(terms) - max(30, domain_cap))
            penalty_terms.append(excess * int(cap_weight))

    # 目标：先"排得下"，再"排得早"，再按软约束加分（重要 / 深度靠上午 / 靠近截止日）
    terms = []
    horizon = max(1, si.days - 1)
    for (task_index, chunk_index, start), var in placed_vars.items():
        task = si.tasks[task_index]
        offset = start // SLOTS_PER_DAY
        reward = 1000
        reward += int(60 * (1 - offset / horizon))  # 越早越好（但远不如"排进去"重要）
        reward += int(_soft_bonus(si, task, start))
        terms.append(var * reward)
    model.Maximize(sum(terms) - sum(penalty_terms))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 3.0
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 0
    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None

    chosen: dict[tuple[int, int], tuple[int, int]] = {}
    for (task_index, chunk_index, start), var in placed_vars.items():
        if solver.Value(var):
            chosen[(task_index, chunk_index)] = (start, sizes[task_index][chunk_index])

    placed = []
    skipped = []
    for task_index, task in enumerate(si.tasks):
        # 排上一部分也算数：长任务跨周是常态（"一周都做不完"），剩余分钟进 `unscheduled`。
        # 目标函数里每块 +1000，本来就是"能排尽排"，这里只是不再一刀切丢掉半条。
        got = 0
        for chunk_index, size in enumerate(sizes[task_index]):
            key = (task_index, chunk_index)
            if key in chosen:
                start, size = chosen[key]
                placed.append((task, start, size))
                got += size
        remaining = max(0, int(task.minutes or 0) - got)
        if remaining > 0:
            skipped.append((task, remaining))
    return _solution_from_blocks(si, placed, "cpsat", skipped, blocked)


def _solve_greedy(si: ScheduleInput, blocked: list[bool]) -> Solution:
    """纯 Python 后备：按截止日排队，优先挑"当天还没排过同域"的最早空档。

    它不保证最优，但**保证不违反硬约束**，而且与 CP-SAT 共用同一套候选点。
    """
    grid = list(blocked)
    deep_cap = int(ir.hard_value(si.constraints, "max_deep_minutes", 240) or 0)
    total_cap = int(ir.hard_value(si.constraints, "max_minutes", DEFAULT_TOTAL_CAP) or DEFAULT_TOTAL_CAP)
    domain_cap = int(si.options.get("maxDomainMinPerDay") or DEFAULT_DOMAIN_CAP)

    order = sorted(
        si.tasks,
        key=lambda one: (one.deadline or date_type.max, not one.important, -one.minutes, one.id),
    )
    placed: list[tuple[Task, int, int]] = []
    skipped: list[tuple[Task, int]] = []
    used: dict[int, dict[str, int]] = defaultdict(lambda: {"deep": 0, "total": 0, "domain": {}})

    for task in order:
        sizes, _tail = plan_chunks(si, task)
        trial = list(grid)
        picks: list[tuple[int, int]] = []
        last_domain: dict[int, str] = {}
        for size in sizes:
            options = _candidates(si, task, size, trial)
            if not options:
                break
            choice = _greedy_choice(si, task, size, options, used, deep_cap, total_cap, domain_cap, last_domain)
            if choice is None:
                break
            index = choice
            for step in range(max(1, size // SLOT)):
                trial[index + step] = True
            offset = index // SLOTS_PER_DAY
            used[offset]["total"] += size
            if task.deep:
                used[offset]["deep"] += size
            used[offset]["domain"][task.domain] = used[offset]["domain"].get(task.domain, 0) + size
            last_domain[offset] = task.domain
            picks.append((index, size))

        # **排上一半也照排**：长任务本来就该跨天、跨周（"一周都做不完"是常态），
        # 只因为它后半段这周放不下就一条都不排，等于把整周的空白留给它。
        for index, size in picks:
            for step in range(max(1, size // SLOT)):
                grid[index + step] = True
            placed.append((task, index, size))
        remaining = max(0, int(task.minutes or 0) - sum(size for _index, size in picks))
        if remaining > 0:
            skipped.append((task, remaining))
    return _solution_from_blocks(si, placed, "greedy", skipped, blocked)


def _greedy_choice(
    si: ScheduleInput,
    task: Task,
    size: int,
    options: list[int],
    used: dict,
    deep_cap: int,
    total_cap: int,
    domain_cap: int,
    last_domain: dict[int, str],
) -> int | None:
    """在三档宽松度里找最靠前、软约束最满意的位置。

    1. 全部偏好都满足（换域、单日单主题上限）；
    2. 允许超过单日单主题上限（软约束，超了只是不理想）；
    3. 连换域也放弃（**硬约束仍然不许破**：总时长、深度时长）。

    这样后备实现不会因为一条软约束就把一个本来能排的任务丢掉。
    """

    def usable(index: int, *, cap: bool, alternate: bool) -> bool:
        day_used = used[index // SLOTS_PER_DAY]
        if day_used["total"] + size > total_cap:
            return False
        if task.deep and day_used["deep"] + size > deep_cap:
            return False
        if cap and task.domain != "other" and day_used["domain"].get(task.domain, 0) + size > domain_cap:
            return False
        return not (alternate and last_domain.get(index // SLOTS_PER_DAY) == task.domain and len(options) > 1)

    for cap, alternate in ((True, True), (False, True), (False, False)):
        best: int | None = None
        best_score = 0.0
        for index in options:
            if not usable(index, cap=cap, alternate=alternate):
                continue
            score = _soft_bonus(si, task, index) - (index // SLOTS_PER_DAY) * 30
            if best is None or score > best_score:
                best, best_score = index, score
        if best is not None:
            return best
    return None


# ============================================================================ 收尾


def _validate(si: ScheduleInput, blocks: list[PlacedBlock]) -> bool:
    """解出来之后**再验一遍硬约束** —— 后备/求解器都走这条，谁也别想蒙混。"""
    cells: dict[int, str] = {}
    for block in blocks:
        first = _cell(si, block.start)
        last = _cell(si, block.end - timedelta(seconds=1))
        for index in range(first, last + 1):
            if index in cells:
                return False
            cells[index] = block.task_id
    deep_cap = int(ir.hard_value(si.constraints, "max_deep_minutes", 240) or 0)
    per_day: dict[int, int] = defaultdict(int)
    for block in blocks:
        if block.focus != "deep":
            continue
        per_day[(_as_local(block.start, si.tz).date() - si.start).days] += int(
            (block.end - block.start).total_seconds() // 60
        )
    return all(minutes <= deep_cap for minutes in per_day.values())


def _break_domain_runs(si: ScheduleInput, solution: Solution) -> Solution:
    """把"同域连排"打散：后一块能换到别的空档就换。

    求解器里没有对"相邻块同域"建模（那需要成对的 0/1 变量，规模会炸），
    所以放在求解之后做一次**确定性的局部改进**：只看相邻（间隔 ≤ 15 分钟）的两块，
    后一块挪到最早的可放位置，挪之前先验硬约束。挪不动就保持原样。

    每次挪完**重新扫一遍**（块的时间顺序变了，接着往下扫会拿到过期的邻居）。
    """

    def cells(size: int) -> int:
        return max(1, size // SLOT)

    if len(solution.blocks) < 2:
        return solution
    blocks = sorted(solution.blocks, key=lambda one: (one.start, one.task_id))
    blocked = _blocked_cells(si)
    for one in blocks:
        first = _cell(si, one.start)
        for step in range(cells(int((one.end - one.start).total_seconds() // 60))):
            if 0 <= first + step < len(blocked):
                blocked[first + step] = True

    moved = 0
    stuck: set[str] = set()
    while moved < 12:
        pair = None
        for index in range(1, len(blocks)):
            previous, current = blocks[index - 1], blocks[index]
            if current.task_id in stuck:
                continue
            if previous.domain != current.domain or previous.domain == "other":
                continue
            if _as_local(current.start, si.tz) - _as_local(previous.end, si.tz) > timedelta(minutes=15):
                continue
            pair = index
            break
        if pair is None:
            break

        current = blocks[pair]
        task = next((one for one in si.tasks if one.id == current.task_id), None)
        if task is None or not task.splittable:
            stuck.add(current.task_id)
            continue
        size = int((current.end - current.start).total_seconds() // 60)
        old = _cell(si, current.start)
        for step in range(cells(size)):
            if 0 <= old + step < len(blocked):
                blocked[old + step] = False
        options = _candidates(si, task, size, blocked)
        target = next((one for one in options if one != old), None)
        if target is None:
            for step in range(cells(size)):
                if 0 <= old + step < len(blocked):
                    blocked[old + step] = True
            stuck.add(current.task_id)
            continue
        for step in range(cells(size)):
            if 0 <= target + step < len(blocked):
                blocked[target + step] = True
        blocks[pair] = PlacedBlock(
            task_id=current.task_id,
            title=current.title,
            start=_slot_start(si, target),
            end=_slot_start(si, target) + timedelta(minutes=size),
            focus=current.focus,
            domain=current.domain,
            reason=_explain(si, task, _slot_start(si, target)),
        )
        blocks.sort(key=lambda one: (one.start, one.task_id))
        moved += 1

    blocks.sort(key=lambda one: (one.start, one.task_id))
    minutes = sum(int((one.end - one.start).total_seconds() // 60) for one in blocks)
    return Solution(
        blocks=blocks,
        unscheduled=solution.unscheduled,
        engine=solution.engine,
        stats={**solution.stats, "minutes": minutes, "alternated": moved},
        constraints=solution.constraints,
    )


__all__ = [
    "DEFAULT_DOMAIN_CAP",
    "DEFAULT_TOTAL_CAP",
    "MAX_CHUNK",
    "FixedBlock",
    "PlacedBlock",
    "ScheduleInput",
    "Solution",
    "Task",
    "build_input",
    "chunks_of",
    "explain_unscheduled",
    "solve",
]
