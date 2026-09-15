"""掌握度与共享夹具的一致性。

`meta/mastery-fixtures.json` 是前后端共同的契约：`tools/selftest.mjs`
断言同一份数据。任一侧改了公式而没同步，这里或那里就会红。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import get_settings
from app.mastery import BAND_LABELS, js_round, mastery_band, mastery_score

ROOT = Path(get_settings().questions_dir).parent
FIXTURES = ROOT / "meta" / "mastery-fixtures.json"


@pytest.fixture(scope="module")
def fixtures() -> dict:
    if not FIXTURES.is_file():
        pytest.fail(f"缺少掌握度夹具：{FIXTURES}")
    return json.loads(FIXTURES.read_text(encoding="utf-8"))


def test_fixtures_cover_the_bands(fixtures: dict) -> None:
    """夹具必须覆盖所有档位，否则某个档位的公式改了也不会被发现。"""
    bands = {case["expectBand"] for case in fixtures["cases"]}
    missing = {"new", "weak", "fair", "solid", "mastered"} - bands
    assert not missing, f"夹具没有覆盖这些档位：{sorted(missing)}"


def _case_ids() -> list[str]:
    """参数化用夹具里真实的用例名 —— 新增用例不需要同步改测试。"""
    data = json.loads(FIXTURES.read_text(encoding="utf-8"))
    return [case["name"] for case in data["cases"]]


@pytest.mark.parametrize("name", _case_ids())
def test_matches_fixture(fixtures: dict, name: str) -> None:
    """逐条比对。用用例名参数化，失败时一眼看出是哪条。"""
    case = next(item for item in fixtures["cases"] if item["name"] == name)
    args = case["input"]

    score = mastery_score(
        attempts=args["attempts"],
        correct=args["correct"],
        streak=args["streak"],
        last_at=args["lastAt"],
        now=args["now"],
    )
    assert score == case["expectScore"], f"{case['name']}（{case['desc']}）"
    assert mastery_band(score, args["attempts"]) == case["expectBand"]
    assert case["expectBand"] in BAND_LABELS


def test_js_round_matches_javascript() -> None:
    """取整必须与 JS 的 Math.round 一致（.5 向上）。

    Python 内置 round() 是银行家舍入：round(0.5)==0、round(2.5)==2，
    会让掌握度在 .5 处比前端少 1 分。
    """
    assert js_round(0.5) == 1
    assert js_round(1.5) == 2
    assert js_round(2.5) == 3
    assert js_round(-0.5) == 0  # JS: Math.round(-0.5) === -0
    assert js_round(46.5) == 47


def test_future_timestamp_does_not_inflate() -> None:
    """lastAt 在未来（客户端时钟偏差）时，新鲜度不能超过 1。

    否则会把 base 乘到大于 1，凭空多出分数。
    """
    now = 1789459200000
    skewed = mastery_score(attempts=3, correct=3, streak=3, last_at=now + 99999999, now=now)
    normal = mastery_score(attempts=3, correct=3, streak=3, last_at=now, now=now)
    assert skewed == normal


def test_score_never_leaves_range() -> None:
    """无论输入多极端，分数都必须落在 0–100。"""
    now = 1789459200000
    for attempts in (1, 5, 100):
        for correct in (0, attempts):
            for streak in (0, 50):
                score = mastery_score(
                    attempts=attempts, correct=correct, streak=streak, last_at=now, now=now
                )
                assert 0 <= score <= 100
