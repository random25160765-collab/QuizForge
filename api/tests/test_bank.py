"""题库导入与读取。

这里最重要的是 `test_bank_payload_matches_offline_dataset`：
它锁定「接口返回的题库」与「离线构建产物」逐字节一致。
前端 `data.install()` 两边吃的是同一份结构，一旦漂移，
会出现「离线版正常、在线版某块界面静静空掉」这类极难定位的问题。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.bank_import import apply, current_bank, scan
from app.config import get_settings
from app.db import get_session_factory
from app.models import BankVersion, Question, Topic

ROOT = Path(__file__).resolve().parents[2]  # 仓库根（不再从 questions_dir 反推：它会指向物化目录）
OFFLINE_DATASET = ROOT / "dist" / "data.json"


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


def test_bank_payload_matches_offline_dataset(imported_bank, db_session) -> None:  # noqa: ANN001
    """接口返回 = 离线产物（除 generatedAt）。

    这是整个改造里最容易悄悄坏掉的一条契约。
    """
    if not OFFLINE_DATASET.is_file():
        pytest.skip("尚未构建离线产物，先跑 python3 tools/build.py")

    offline = json.loads(OFFLINE_DATASET.read_text(encoding="utf-8"))
    api = current_bank(db_session)

    assert [q["id"] for q in api["questions"]] == [q["id"] for q in offline["questions"]]
    assert api["questions"] == offline["questions"]
    assert api["meta"]["topics"] == offline["meta"]["topics"]
    assert api["meta"]["groups"] == offline["meta"]["groups"]
    assert api["meta"]["stats"] == offline["meta"]["stats"]
    assert api["meta"]["typeLabels"] == offline["meta"]["typeLabels"]


def test_bank_requires_login(client) -> None:  # noqa: ANN001
    client.cookies.clear()
    assert client.get("/api/bank").status_code == 401


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


def _ensure_logged_in(client, email: str) -> None:  # noqa: ANN001
    """注册或登录，保证这个 client 已持有会话。

    注册回 201、登录回 200，断言「已登录」比断言某一个状态码更贴近意图。
    """
    client.cookies.clear()
    resp = client.post("/api/auth/register", json={"email": email, "password": "password-1234"})
    if resp.status_code == 409:
        client.cookies.clear()
        resp = client.post("/api/auth/login", json={"email": email, "password": "password-1234"})
    assert resp.status_code in (200, 201), resp.text
    assert client.get("/api/auth/me").status_code == 200


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
