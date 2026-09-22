"""进度同步的领域逻辑。

从 `routers/progress.py` 里抽出来，让路由只管 HTTP（参数校验、错误码），
这里的函数只关心「数据怎么合」。

## 为什么计数必须来自流水

旧实现让客户端上传记录对象，按 `client_rev` 做 last-write-wins。
问题是客户端上传的是「它本地看到的累计值」——浏览器里那份乐观缓存离线攒了一阵，
回传时若 rev 已被另一侧超过，这段作答就被**静默丢弃**：计数不增，用户毫无察觉。

现在改成：每次判分产生一条**流水**（增量），服务端只对**真正插入成功**的
流水累加。插入是精确一次的，累加就天然正确，多批提交直接相加而不是取较大值。

## 为什么 `ON CONFLICT DO NOTHING` + `RETURNING` 是这里的核心

「请求发出、响应丢失、客户端重发」在网络里是常态。把幂等交给数据库
主键约束（流水的 id 由客户端生成），再用 `RETURNING` 拿到**实际落库的行**，
就不用再写一套「查一下有没有、再决定加不加」的逻辑 ——
那套逻辑在并发下本身就是错的。
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import UTC, date as date_type, datetime
from typing import Any

from sqlalchemy import case, func, select, type_coerce
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session as OrmSession

from .models import Attempt, DayStat, Record, Topic

# 客户端**不得**上传的字段：这些只能由流水推导。
# 一旦允许上传，一台设备的快照就会覆盖另一台的增量，等于问题没解决。
COUNTER_INPUT_FIELDS = frozenset(
    {
        "attempts",
        "correct",
        "partial",
        "wrong",
        "firstAt",
        "lastAt",
        "lastStatus",
        "lastScore",
        "id",
        "_rev",
    }
)

# 客户端补丁：主观状态，没有可加和的语义，按 client_rev 的 LWW 合并。
# 存进 `patch` JSONB 列。
PATCH_FIELDS = ("sm2", "note", "streak", "parts")
# 有独立列、但同样归补丁通道的字段。
# `lastResponse` 属于这里而不是计数：`applyResult` 在「待批改」分支也会写它，
# 而那个分支**不产生流水** —— 若把它算作流水推导，用户提交的作答内容会丢。
PATCH_COLUMN_FIELDS = ("lastResponse",)
ACTION_FIELDS = ("mastered", "flagged")

# 补丁字段名 → 数据库列名。只有「有独立列」的那些字段需要在这里登记。
_COLUMN_BY_PATCH_FIELD = {"lastResponse": "last_response"}

VALID_STATUS = frozenset({"correct", "partial", "wrong"})
_COUNTER_BY_STATUS = {"correct": "correct", "partial": "partial", "wrong": "wrong"}

# 时间的合理范围。超出就直接丢弃这条流水 ——
# 否则 datetime.fromtimestamp 会抛 ValueError，整批作答一起 500，
# 与「一条脏数据不该让用户这次的全部作答都存不上」相违。
_MIN_AT_MS = 946684800000        # 2000-01-01
_MAX_AT_MS = 7258118400000       # 2200-01-01

# 一次同步最多接受多少条流水：防止一个坏客户端把库撑爆
MAX_ATTEMPTS_PER_SYNC = 2000


# --------------------------------------------------------------------- 组装


def compose_record(row: Record) -> dict:
    """把一行记录组装成前端认识的形状。

    前端 `store.js` 里的记录对象是这个形状，所以 `hydrate` 灌进去之后，
    89 处 `store.*` 调用点一行都不用改。
    """
    patch = row.patch or {}
    return {
        "id": row.question_id,
        # ---- 计数：来自流水 ----
        "attempts": row.attempts,
        "correct": row.correct,
        "partial": row.partial,
        "wrong": row.wrong,
        "firstAt": row.first_at,
        "lastAt": row.last_at,
        "lastStatus": row.last_status,
        "lastScore": row.last_score,
        "lastResponse": row.last_response,
        # ---- 补丁：来自客户端 ----
        "sm2": patch.get("sm2"),
        "note": patch.get("note") or "",
        "streak": int(patch.get("streak") or 0),
        # 大题各小问的批改结果：错题本要靠它还原每一问的讲评
        "parts": patch.get("parts"),
        "mastered": row.mastered,
        "flagged": row.flagged,
        "_rev": row.client_rev,
    }


def compose_day(row: DayStat) -> dict:
    return {"answers": row.answers, "correct": row.correct, "topics": row.topics or {}}


# ------------------------------------------------------------------ 流水解析


def parse_attempts(raw: Any) -> tuple[list[dict], int]:
    """把客户端传来的流水规整成可入库的行。返回 (有效行, 丢弃数)。

    丢掉而不是整批拒绝：一条脏数据不该让用户这次的全部作答都存不上。
    """
    if not isinstance(raw, list):
        return [], 0

    rows: list[dict] = []
    dropped = 0
    for item in raw[:MAX_ATTEMPTS_PER_SYNC]:
        row = _parse_one(item)
        if row is None:
            dropped += 1
            continue
        rows.append(row)
    if len(raw) > MAX_ATTEMPTS_PER_SYNC:
        dropped += len(raw) - MAX_ATTEMPTS_PER_SYNC
    return rows, dropped


def _parse_one(item: Any) -> dict | None:
    if not isinstance(item, dict):
        return None
    try:
        attempt_id = uuid.UUID(str(item.get("id")))
    except (ValueError, AttributeError, TypeError):
        return None

    question_id = str(item.get("questionId") or "").strip()
    status = str(item.get("status") or "").strip()
    if not question_id or status not in VALID_STATUS:
        return None

    at_ms = _as_int(item.get("at"))
    if at_ms is None or not (_MIN_AT_MS <= at_ms <= _MAX_AT_MS):
        return None

    day = _as_day(item.get("day")) or datetime.fromtimestamp(at_ms / 1000, tz=UTC).date()

    return {
        "id": attempt_id,
        "questionId": question_id,
        "atMs": at_ms,
        "day": day,
        "status": status,
        "score": _as_float(item.get("score")) or 0.0,
        "response": item.get("response"),
        "topicKey": str(item.get("topicKey") or "").strip()[:64],
    }


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _as_day(value: Any) -> date_type | None:
    if isinstance(value, date_type):
        return value
    if isinstance(value, str):
        try:
            return date_type.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------- 流水的写入


def insert_attempts(db: OrmSession, rows: list[dict]) -> list[dict]:
    """幂等插入流水，返回**本次真正新插入**的那些。

    返回新插入的行是关键：调用方据此累加计数与每日统计。
    重发（id 已存在）的行不会出现在结果里，因此不会被重复计数。
    """
    if not rows:
        return []

    values = [
        {
            "id": row["id"],
            "question_id": row["questionId"],
            "at": datetime.fromtimestamp(row["atMs"] / 1000, tz=UTC),
            "day": row["day"],
            "status": row["status"],
            "score": row["score"],
            "response": row["response"],
            "topic_key": row["topicKey"],
        }
        for row in rows
    ]

    inserted = db.execute(
        sqlite_insert(Attempt)
        # 主键是客户端生成的 id，所以重发天然落到 DO NOTHING 上
        .on_conflict_do_nothing(index_elements=["id"])
        .returning(
            Attempt.id,
            Attempt.question_id,
            Attempt.at,
            Attempt.day,
            Attempt.status,
            Attempt.score,
            Attempt.response,
            Attempt.topic_key,
        )
        .values(values)
    ).all()

    return [
        {
            "id": row.id,
            "questionId": row.question_id,
            "atMs": int(row.at.timestamp() * 1000),
            "day": row.day,
            "status": row.status,
            "score": row.score,
            "response": row.response,
            "topicKey": row.topic_key,
        }
        for row in inserted
    ]


# ------------------------------------------------------------------ 计数累加


def bump_records(db: OrmSession, attempts: list[dict]) -> None:
    """把新插入的流水累加进 `records`。

    先按题目聚合，一道题只发一条 `UPDATE`：既减少往返，也让「同批次内乱序」
    自动消失（求和可交换，min/max 也是）。

    乱序**跨批次**到达（离线期间攒下的旧流水补传）由 SQL 里的 `LEAST / GREATEST /
    CASE WHEN :at >= last_at` 兜住：第一次作答的时间只会更早、最近作答只会更晚，
    且补传的旧流水不会把 `last_status` 改回过去 —— 否则界面上的判定条会显示
    一个已经过期的答案。
    """
    if not attempts:
        return

    grouped: dict[str, dict] = {}
    for item in attempts:
        group = grouped.setdefault(
            item["questionId"],
            {
                "n": 0,
                "correct": 0,
                "partial": 0,
                "wrong": 0,
                "first": item["atMs"],
                "last": item["atMs"],
                "latest": item,
            },
        )
        group["n"] += 1
        group[_COUNTER_BY_STATUS[item["status"]]] += 1
        group["first"] = min(group["first"], item["atMs"])
        group["last"] = max(group["last"], item["atMs"])
        if item["atMs"] >= group["latest"]["atMs"]:
            group["latest"] = item

    for question_id, group in grouped.items():
        latest = group["latest"]
        db.execute(
            sqlite_insert(Record)
            .values(
                question_id=question_id,
                attempts=group["n"],
                correct=group["correct"],
                partial=group["partial"],
                wrong=group["wrong"],
                first_at=group["first"],
                last_at=group["last"],
                last_status=latest["status"],
                last_score=latest["score"],
                last_response=latest["response"],
                patch={},
                client_rev=0,
            )
            .on_conflict_do_update(
                index_elements=["question_id"],
                set_={
                    # 计数是纯增量，直接相加
                    "attempts": Record.attempts + group["n"],
                    "correct": Record.correct + group["correct"],
                    "partial": Record.partial + group["partial"],
                    "wrong": Record.wrong + group["wrong"],
                    # first_at 初值是 0（「还没作答过」），不能直接取较小值，
                    # 否则 min(0, x) 永远是 0
                    "first_at": case(
                        (Record.first_at == 0, group["first"]),
                        else_=case(
                            (Record.first_at < group["first"], Record.first_at),
                            else_=group["first"],
                        ),
                    ),
                    # 取较晚的那个。用 `case` 而不是 `greatest`：SQLite 没有
                    # greatest（那是 Postgres 的），scalar 的 `max(a, b)` 在
                    # Postgres 上又是聚合函数 —— `case` 两边都对。
                    "last_at": case(
                        (Record.last_at > group["last"], Record.last_at),
                        else_=group["last"],
                    ),
                    # 只有这批流水确实更新时才改「最近一次」的各项。
                    # SQL 的 SET 表达式看到的都是旧值，所以这里的比较是安全的。
                    "last_status": case(
                        (group["last"] >= Record.last_at, latest["status"]),
                        else_=Record.last_status,
                    ),
                    "last_score": case(
                        (group["last"] >= Record.last_at, latest["score"]),
                        else_=Record.last_score,
                    ),
                    # last_response 与 last_status 同步更新：
                    # 两者若来自不同的那次作答，界面上就会出现
                    # 「判定说这题答对了，展示的却是一个错答案」。
                    # 补丁通道**也**能写它，那是为了「待批改」—— 那种提交不产生
                    # 流水，只由流水驱动的字段装不下用户填的内容。
                    #
                    # 这里必须**带上列的类型**（`type_coerce`）：`last_response` 是
                    # JSON 列，而 CASE 分支里的裸值不会经过 JSON 序列化 ——
                    # 直接塞字符串会把 `C` 而不是 `"C"` 写进去，读的时候 JSON 解不开
                    # （实测：`JSONDecodeError: Expecting value: line 1 column 1`）。
                    # 用 type_coerce 而不是 cast：只带类型、不生成 SQL 里的 CAST。
                    "last_response": case(
                        (
                            group["last"] >= Record.last_at,
                            type_coerce(latest["response"], Record.last_response.type),
                        ),
                        else_=Record.last_response,
                    ),
                    # client_rev 只能由 apply_patches 维护
                },
            )
        )


def bump_days(db: OrmSession, attempts: list[dict]) -> None:
    """把新插入的流水累加进 `days`。

    累加而不是取较大值：同一天分几批提交（做完一批、隔一会儿又做一批）各算各的。

    先用 `ON CONFLICT DO NOTHING` 确保行存在，再读改写。
    `topics` 的键是动态的（学科名），JSON 列没有「按路径自增」的原子写法，
    所以只能「读出来 + 改 + 写回去」。

    并发：这一整段本来就在一个写事务里，而 SQLite 的写事务是**全库排他**的 ——
    不需要再 `FOR UPDATE`（SQLite 也没有行锁）。所以是"读改写"。
    """
    if not attempts:
        return

    subjects = _resolve_subjects(db, {item["topicKey"] for item in attempts if item["topicKey"]})

    per_day: dict[date_type, dict] = defaultdict(
        lambda: {"answers": 0, "correct": 0, "topics": defaultdict(int)}
    )
    for item in attempts:
        agg = per_day[item["day"]]
        agg["answers"] += 1
        if item["status"] == "correct":
            agg["correct"] += 1
        subject = subjects.get(item["topicKey"], "")
        if subject:
            agg["topics"][subject] += 1

    for day, agg in per_day.items():
        db.execute(
            sqlite_insert(DayStat)
            .values(date=day, answers=0, correct=0, topics={})
            .on_conflict_do_nothing(index_elements=["date"])
        )
        row = db.execute(
            select(DayStat).where(DayStat.date == day)
        ).scalar_one()

        row.answers += agg["answers"]
        row.correct += agg["correct"]
        merged = dict(row.topics or {})
        for subject, count in agg["topics"].items():
            merged[subject] = int(merged.get(subject) or 0) + count
        row.topics = merged


def apply_days_seed(db: OrmSession, seed: Any) -> list[str]:
    """导入备份时用的「历史热力图种子」。

    导入进来的每日统计是**绝对值**且没有对应流水，所以既不能走累加
    （那要靠流水），也不能像旧实现那样无条件覆盖（会把真实统计抹掉）。

    折中办法是**按日期把关**：只接受「这一天还没有任何流水」的日期，
    并且取较大值。于是：
      * 从离线版迁移过来（服务端零流水）→ 历史热力图完整保留
      * 已经有真实作答的日期 → 种子被忽略，不会被伪造的数字盖掉
      * 同一份种子重发多次 → 结果不变（取较大值是幂等的）
    """
    if not isinstance(seed, dict):
        return []

    busy_dates = set(db.scalars(select(Attempt.day).distinct()))
    accepted: list[str] = []

    for key, value in seed.items():
        day = _as_day(key)
        if day is None or day in busy_dates or not isinstance(value, dict):
            continue

        db.execute(
            sqlite_insert(DayStat)
            .values(date=day, answers=0, correct=0, topics={})
            .on_conflict_do_nothing(index_elements=["date"])
        )
        row = db.execute(
            select(DayStat).where(DayStat.date == day)
        ).scalar_one()

        row.answers = max(row.answers, _as_int(value.get("answers")) or 0)
        row.correct = max(row.correct, _as_int(value.get("correct")) or 0)
        merged = dict(row.topics or {})
        for subject, count in (value.get("topics") or {}).items():
            merged[str(subject)] = max(
                _as_int(merged.get(subject)) or 0, _as_int(count) or 0
            )
        row.topics = merged
        accepted.append(day.isoformat())

    db.flush()
    return accepted


def _resolve_subjects(db: OrmSession, topic_keys: set[str]) -> dict[str, str]:
    """把知识点 key 映射到它所属的一级主题（热力图按学科统计）。

    服务端解析而不是让客户端上报：主题树是服务端的事实，
    客户端多传一个字段就多一处可能不一致的地方。
    查不到的 key 返回空串，调用方跳过（退役主题的历史流水不该报错）。
    """
    if not topic_keys:
        return {}
    rows = db.execute(
        select(Topic.key, Topic.path).where(Topic.key.in_(sorted(topic_keys)))
    ).all()
    return {key: (path[0] if path else "") for key, path in rows}


# ------------------------------------------------------------------ 补丁通道


def apply_patches(db: OrmSession, patches: Any) -> dict[str, dict]:
    """按 `client_rev` 合并补丁，返回被拒绝的那些（附权威值）。

    **彻底忽略计数字段**：客户端即便上传 `attempts/correct/...` 也不会被采纳。
    这条硬约束是整个改造的前提 —— 只要还允许上传计数，
    浏览器里那份乐观缓存就会把服务端的增量覆盖掉。
    """
    if not isinstance(patches, dict):
        return {}

    rejected: dict[str, dict] = {}
    for question_id, incoming in patches.items():
        if not isinstance(incoming, dict):
            continue
        question_id = str(question_id)[:96]
        rev = _as_int(incoming.get("_rev")) or 0

        sanitized = {key: incoming.get(key) for key in PATCH_FIELDS if key in incoming}
        actions = {key: bool(incoming.get(key)) for key in ACTION_FIELDS if key in incoming}
        # 有独立列的补丁字段：`lastResponse` 映射到 `last_response` 列
        column_patch: dict[str, Any] = {}
        for key in PATCH_COLUMN_FIELDS:
            if key in incoming:
                column_patch[_COLUMN_BY_PATCH_FIELD[key]] = incoming[key]

        row = db.get(Record, question_id)
        if row is None:
            # 补丁可能先于流水到达（客户端只标了星标、还没作答）。
            # 计数留 0 —— 它们只能由流水填。
            db.add(
                Record(
                    question_id=question_id,
                    patch=sanitized,
                    client_rev=rev,
                    **actions,
                    **column_patch,
                )
            )
            continue

        if rev <= row.client_rev:
            rejected[question_id] = compose_record(row)
            continue

        if sanitized:
            row.patch = {**(row.patch or {}), **sanitized}
        for key, value in {**actions, **column_patch}.items():
            setattr(row, key, value)
        row.client_rev = rev

    db.flush()
    return rejected


# ------------------------------------------------------------------ 重置基线


def apply_resets(db: OrmSession, resets: Any) -> list[str]:
    """「重新设基线」：直接设定计数，不动流水。

    **刻意不删流水**。流水是幂等键的载体：删掉之后，客户端重发一条旧流水
    就会重新插入并再累加一次，反而多算了。保留它，重发自然落到
    `ON CONFLICT DO NOTHING` 上。

    代价是重置后 `records.attempts` 不再等于该题流水条数。这正是本方案
    选择「增量累加」而非「全量重算」的必然结果，也是它有意的取舍。

    传 `null` 表示清零。
    """
    if not isinstance(resets, dict):
        return []

    touched: list[str] = []
    for question_id, value in resets.items():
        question_id = str(question_id)[:96]
        row = db.get(Record, question_id)

        if value is None:
            if row is not None:
                row.attempts = 0
                row.correct = 0
                row.partial = 0
                row.wrong = 0
                row.first_at = 0
                row.last_status = ""
                row.last_score = 0.0
                row.last_response = None
                touched.append(question_id)
            continue

        if not isinstance(value, dict):
            continue

        if row is None:
            row = Record(question_id=question_id, patch={})
            db.add(row)

        row.attempts = _as_int(value.get("attempts")) or 0
        row.correct = _as_int(value.get("correct")) or 0
        row.partial = _as_int(value.get("partial")) or 0
        row.wrong = _as_int(value.get("wrong")) or 0
        row.first_at = _as_int(value.get("firstAt")) or 0
        row.last_at = _as_int(value.get("lastAt")) or 0
        row.last_status = str(value.get("lastStatus") or "")
        row.last_score = _as_float(value.get("lastScore")) or 0.0
        row.last_response = value.get("lastResponse")
        touched.append(question_id)

    db.flush()
    return touched


# -------------------------------------------------------------------- 快照


def snapshot(db: OrmSession) -> dict:
    records = {
        row.question_id: compose_record(row) for row in db.scalars(select(Record)).all()
    }
    days = {
        row.date.isoformat(): compose_day(row) for row in db.scalars(select(DayStat)).all()
    }
    return {"records": records, "days": days}


def attempt_count(db: OrmSession) -> int:
    return db.scalar(select(func.count()).select_from(Attempt)) or 0


def wipe(db: OrmSession) -> None:
    """清空全部学习数据（流水一并删除）。

    这是唯一该删流水的场合：整份学习数据都被清掉了，幂等键也就没有意义了。
    """
    for model in (Attempt, Record, DayStat):
        for row in db.scalars(select(model)).all():
            db.delete(row)
    db.flush()
