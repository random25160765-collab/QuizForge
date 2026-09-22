"""题库导入与读取。

这里最重要的是 `test_bank_payload_round_trips_through_db`：
它锁定「导入时那份数据集」与「从库读回来的接口响应」逐字节一致。
前端 `data.install()` 吃的就是读回来的这一份，一旦漂移，
会出现「某块界面静静空掉」这类极难定位的问题。
"""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import func, select

from app.bank_import import apply, current_bank, scan
from app.config import get_settings
from app.db import get_session_factory
from app.models import BankVersion, Question, Topic


# `imported_bank` 夹具在 conftest.py —— 不要在这里再定义一份：
# 模块内定义会遮蔽 conftest 的，两边各建一次缓存，第二次导入就只剩「未变」，
# 夹具里的「首次导入应当全部是新增」断言会直接失败。


# ------------------------------------------------------------------ 扫描


def test_scan_parses_every_question() -> None:
    result = scan()
    assert result.file_count == len(result.questions)
    assert len(result.questions) >= 1
    assert not result.errors


def test_scan_is_deterministic() -> None:
    """同源码两次扫描必须得到同一个版本指纹，否则 ETag 会毫无意义地抖动。"""
    assert scan().content_hash == scan().content_hash


def test_scan_does_not_touch_database(engine) -> None:  # noqa: ANN001
    """扫描是纯函数：连库都不该连（预演与校验都靠这个性质）。"""
    before = _counts()
    scan()
    assert _counts() == before


# ------------------------------------------------------------------ 导入


def test_import_is_idempotent(imported_bank, db_session) -> None:  # noqa: ANN001
    """原样再导一次：全部「未变」，且不新增版本行。

    早先的实现每次导入都插一条版本行，撞上 content_hash 唯一约束直接报错 ——
    版本应当由内容标识，而不是由「导入过几次」标识。
    """
    versions_before = db_session.scalar(select(func.count()).select_from(BankVersion))
    report = apply(db_session, imported_bank)
    assert report.added == []
    assert report.updated == []
    assert report.retired == []
    assert len(report.unchanged) == len(imported_bank.questions)
    assert db_session.scalar(select(func.count()).select_from(BankVersion)) == versions_before


def test_dry_run_writes_nothing(imported_bank, db_session) -> None:  # noqa: ANN001
    """预演只算不写。

    分两步实现（先算增量、再落库）就是为了这条：调用方不需要记得回滚，
    预演本身也不会留下任何痕迹。
    """
    victim = imported_bank.questions[-1]["id"]
    partial = _subset(imported_bank, drop={victim})

    versions_before = db_session.scalar(select(func.count()).select_from(BankVersion))
    report = apply(db_session, partial, dry_run=True)

    assert victim in report.retired, "预演依然要报出影响面"

    db_session.expire_all()
    assert db_session.get(Question, victim).retired_at is None, "预演不该改数据"
    assert db_session.scalar(select(func.count()).select_from(BankVersion)) == versions_before


def test_retired_question_is_marked_not_deleted(imported_bank, db_session) -> None:  # noqa: ANN001
    """源里删掉的题只置 retired_at。

    物理删除会让挂在题目 id 上的做题记录变成孤儿 ——
    掌握度、错题本、历史统计都会莫名其妙地少东西。
    """
    victim = imported_bank.questions[-1]["id"]
    partial = _subset(imported_bank, drop={victim})

    report = apply(db_session, partial)
    assert victim in report.retired

    row = db_session.get(Question, victim)
    assert row is not None, "退役不能变成删除"
    assert row.retired_at is not None


def test_restored_question_clears_retired_flag(imported_bank, db_session) -> None:  # noqa: ANN001
    victim = imported_bank.questions[-1]["id"]
    apply(db_session, _subset(imported_bank, drop={victim}))
    assert db_session.get(Question, victim).retired_at is not None

    report = apply(db_session, imported_bank)
    assert victim in report.restored
    assert db_session.get(Question, victim).retired_at is None


def test_import_rejects_invalid_source(imported_bank, db_session) -> None:  # noqa: ANN001
    """有校验错误时拒绝导入，且不留下半成品。"""
    broken = _with_error(imported_bank)

    before = db_session.scalar(select(func.count()).select_from(Question))
    report = apply(db_session, broken)
    assert report.errors, "应当报错并拒绝"
    assert db_session.scalar(select(func.count()).select_from(Question)) == before


# ------------------------------------------------------------------ 接口


def _as_web(value: object) -> object:
    """归一到「浏览器看到的样子」再比。

    两处已知且无害的差异（都只在 Python 层看得见）：

    * `stats.byDifficulty` 的键：文件侧数据集用的是字符串（`'4'`），库侧回的是整数。
      JSON 里键一律是字符串，前端看到的完全一样。
    * `stats.subjectCount`：只出现在文件侧数据集里，接口不回它，前端也不用它
      （首页的覆盖范围走 `/api/health` 的 `subjectList`）。
    """
    out = json.loads(json.dumps(value, sort_keys=True))

    def norm_stats(stats: dict) -> None:
        stats.pop("subjectCount", None)
        stats["byDifficulty"] = {str(k): v for k, v in (stats.get("byDifficulty") or {}).items()}

    if isinstance(out, dict):
        # 两种调用方式都要顾到：传整个 meta，或直接传 meta.stats
        if isinstance(out.get("stats"), dict):
            norm_stats(out["stats"])
        if "byDifficulty" in out:
            norm_stats(out)
    return out


def test_bank_payload_round_trips_through_db(imported_bank, db_session) -> None:  # noqa: ANN001
    """入库再读出来 = 导入时的那份数据集（除 generatedAt）。

    以前这条是与「离线构建产物 `dist/data.json`」比 —— 那份产物已随离线形态淘汰，
    而且它要么缺失（测试静默跳过）、要么是旧的（等于在与历史比对）。
    真正要守的契约没变：同一套解析器产出的 dataset，写进库再由 `current_bank`
    组装回来必须逐字段相同。前端 `data.install()` 吃的正是读回来的这一份。
    """
    scanned = imported_bank.dataset
    api = current_bank(db_session)

    assert [q["id"] for q in api["questions"]] == [q["id"] for q in scanned["questions"]]
    assert _as_web({"q": api["questions"]}) == _as_web({"q": scanned["questions"]})
    for key in ("topics", "groups", "stats", "typeLabels"):
        assert _as_web(api["meta"][key]) == _as_web(scanned["meta"][key]), key


def test_bank_never_needs_login(client) -> None:  # noqa: ANN001
    """本地单用户：不带任何凭据就能读题库。

    （原先这条断言的是"没登录 → 401"；账号面删掉之后它反过来 —— 见 `app/deps.py`。）
    """
    client.cookies.clear()
    assert client.get("/api/bank").status_code == 200


def test_bank_returns_body_and_etag(client, imported_bank) -> None:  # noqa: ANN001
    _ensure_logged_in(client, f"bank-{imported_bank.content_hash[:6]}@example.com")

    first = client.get("/api/bank")
    assert first.status_code == 200
    body = first.json()
    assert len(body["questions"]) == len(imported_bank.questions)
    etag = first.headers["etag"]
    assert etag

    second = client.get("/api/bank", headers={"If-None-Match": etag})
    assert second.status_code == 304
    assert second.content == b""


def test_bank_version_endpoint(client, imported_bank) -> None:  # noqa: ANN001
    _ensure_logged_in(client, f"ver-{imported_bank.content_hash[:6]}@example.com")

    version = client.get("/api/bank/version").json()
    assert version["hash"] == imported_bank.content_hash
    assert version["questions"] == len(imported_bank.questions)


# ------------------------------------------------------------------ 工具


def _ensure_logged_in(client, email: str = "") -> None:  # noqa: ANN001
    """本机用户就绪。

    （原先这里是"注册或登录，保证这个 client 已持有会话"。单用户本地形态
      既没有注册也没有登录 —— 见 `app/deps.py`；名字留着是为了不动调用点。）
    """
    return ""


def _counts() -> tuple[int, int]:
    session = get_session_factory()()
    try:
        return (
            session.scalar(select(func.count()).select_from(Question)) or 0,
            session.scalar(select(func.count()).select_from(Topic)) or 0,
        )
    finally:
        session.close()


def _subset(result, *, drop: set[str]):  # noqa: ANN001, ANN202
    """复制一份 ScanResult，但去掉若干题目 —— 用来模拟「源里删了题」。"""
    from dataclasses import replace

    questions = [q for q in result.questions if q["id"] not in drop]
    dataset = json.loads(json.dumps(result.dataset))
    dataset["questions"] = questions
    return replace(result, dataset=dataset)


def _with_error(result):  # noqa: ANN001, ANN202
    """复制一份带校验错误的 ScanResult —— 用来模拟源里有坏文件。"""
    from dataclasses import replace

    from app.toolkit import Diagnostic

    diag = Diagnostic("ERROR", Path("questions/broken.md"), 1, "示例校验错误")
    return replace(result, diagnostics=list(result.diagnostics) + [diag])
