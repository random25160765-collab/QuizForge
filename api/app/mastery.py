"""掌握度的**规范实现**（Python）。

前端 `store.js` 里有一份等价的 JS 实现，用于即时反馈（点了选项立刻要看到
掌握度变化，等一个来回的网络请求会让交互变钝）。两份实现必须有完全一致的结果，
否则会出现「界面上显示已掌握、服务端统计却说薄弱」这种互相矛盾的展示。

锁住一致性的方式是 `meta/mastery-fixtures.json`：一组校准输入与期望输出，
`pytest` 与 `tools/selftest.mjs` 同时断言同一份夹具，任一侧漂移就立刻失败。

## 两条容易踩的坑

1. **取整方式**：JS 的 `Math.round` 是「四舍五入（.5 向上）」，而 Python 内置
   `round()` 是银行家舍入（`round(0.5) == 0`）。直接用 `round()` 会在
   `.5` 上差 1 分，所以这里显式用 `floor(x + 0.5)`。
2. **时间基准**：`lastAt` 与 `now` 都是 epoch 毫秒。时区不参与计算，
   所以两侧结果不受运行环境影响。
"""

from __future__ import annotations

import math

# 半衰期：30 天不碰，记忆新鲜度减半
HALFLIFE_DAYS = 30.0
MS_PER_DAY = 86400000.0

# 档位阈值（与前端 MASTERY_BANDS 保持一致）
BANDS = (
    ("weak", 40),
    ("fair", 70),
    ("solid", 90),
    ("mastered", 101),  # 上界开区间，用 101 表示「≥90 且 <101」
)

BAND_LABELS = {
    "new": "未练",
    "weak": "薄弱",
    "fair": "一般",
    "solid": "熟练",
    "mastered": "精通",
}


def js_round(value: float) -> int:
    """模拟 JS 的 `Math.round`：`.5` 一律向上。

    Python 的 `round()` 是银行家舍入（round(2.5)==2、round(3.5)==4），
    用它会与前端差 1 分。
    """
    return int(math.floor(value + 0.5))


def mastery_score(
    *,
    attempts: int,
    correct: int,
    streak: int = 0,
    last_at: int = 0,
    now: int,
) -> int:
    """掌握度 0–100。`now` 必填，避免在库函数里偷偷读系统时间。"""
    if not attempts:
        return 0

    accuracy = (correct + 1) / (attempts + 2)
    evidence = attempts / (attempts + 3)
    days = max(0.0, (now - (last_at or now)) / MS_PER_DAY)
    freshness = 0.5 ** (days / HALFLIFE_DAYS)

    base = accuracy * (0.55 + 0.45 * freshness) * (0.6 + 0.4 * evidence)
    bonus = min(streak or 0, 3) * 3
    return max(0, min(100, js_round(base * 100 + bonus)))


def mastery_band(score: int, attempts: int) -> str:
    if not attempts:
        return "new"
    for key, upper in BANDS:
        if score < upper:
            return key
    return "mastered"


def from_record(record: dict | None, now: int) -> tuple[int, str]:
    """从一条前端记录（record payload）算出 (分数, 档位)。"""
    if not record or not record.get("attempts"):
        return 0, "new"
    score = mastery_score(
        attempts=int(record.get("attempts") or 0),
        correct=int(record.get("correct") or 0),
        streak=int(record.get("streak") or 0),
        last_at=int(record.get("lastAt") or 0),
        now=now,
    )
    return score, mastery_band(score, int(record.get("attempts") or 0))
