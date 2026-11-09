"""行级双向同步：用一台**假 Turso**（真的 HTTP，真的 Hrana 形状）验。

真 Turso 的协议只有 `/v2/pipeline` 一个入口，请求/响应都是那几种带
类型标记的 JSON。这里照着它写一个 sqlite3 后端的小服务：
既能验我们自己的同步逻辑，也能验 `TursoClient` 的解析 ——
不用联网，也不会碰到真数据库里的任何东西。

两条纪律贯穿这些用例：
1. **对话只以密文出去**（`enc=1` 且 payload 里搜不到正文）；
2. **拉下来的行不能再被标成脏**（触发器在应用期间静默）。
"""

from __future__ import annotations

import base64
import json
import sqlite3
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from app import autosync, library_cloud, memory, settings_file, sync
from app.db import Base
from app.models import (
    Attempt,
    Conversation,
    Material,
    MaterialSlice,
    Message,
    Plan,
    Record,
    ScheduleEvent,
)


# ============================================================================ 假 Turso


class FakeTurso:
    """一个 sqlite3 支撑的 `/v2/pipeline`。"""

    def __init__(self) -> None:
        self.db = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self.requests: list[dict] = []

        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args) -> None:  # noqa: ANN002 - 静音
                pass

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                if self.path != "/v2/pipeline":
                    self._send(404, {"error": "no such path"})
                    return
                if self.headers.get("Authorization") != "Bearer test-token":
                    self._send(401, {"error": "bad token"})
                    return
                with fake.lock:
                    fake.requests.append(body)
                    results = fake.run(body.get("requests") or [])
                self._send(200, {"results": results})

            def _send(self, status: int, payload: dict) -> None:
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.db.close()

    # ---- 协议 -------------------------------------------------------------

    @staticmethod
    def _arg(value: dict) -> object:
        kind = value.get("type") if isinstance(value, dict) else None
        if kind == "null":
            return None
        if kind == "integer":
            return int(value["value"])
        if kind == "float":
            return float(value["value"])
        if kind == "blob":
            return base64.b64decode(value.get("base64") or "")
        return value.get("value")

    @staticmethod
    def _cell(value: object) -> dict:
        if value is None:
            return {"type": "null"}
        if isinstance(value, bool):
            return {"type": "integer", "value": "1" if value else "0"}
        if isinstance(value, int):
            return {"type": "integer", "value": str(value)}
        if isinstance(value, float):
            return {"type": "float", "value": value}
        if isinstance(value, (bytes, bytearray)):
            return {"type": "blob", "base64": base64.b64encode(bytes(value)).decode("ascii")}
        return {"type": "text", "value": str(value)}

    def run(self, requests: list[dict]) -> list[dict]:
        results: list[dict] = []
        for request in requests:
            if request.get("type") == "close":
                results.append({"type": "ok", "response": {"type": "close"}})
                continue
            stmt = request.get("stmt") or {}
            sql = stmt.get("sql") or ""
            args = [self._arg(value) for value in stmt.get("args") or []]
            try:
                cursor = self.db.execute(sql, args)
                rows = cursor.fetchall() if cursor.description else []
                cols = (
                    [{"name": column[0]} for column in cursor.description]
                    if cursor.description
                    else []
                )
                results.append(
                    {
                        "type": "ok",
                        "response": {
                            "type": "execute",
                            "result": {
                                "cols": cols,
                                "rows": [[self._cell(value) for value in row] for row in rows],
                                "affected_row_count": cursor.rowcount,
                            },
                        },
                    }
                )
            except sqlite3.Error as exc:
                try:
                    self.db.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                results.append({"type": "error", "error": {"message": str(exc)}})
                break
        return results

    # ---- 看内容 -----------------------------------------------------------

    def rows(self, sql: str, args: tuple = ()) -> list[dict]:
        with self.lock:
            cursor = self.db.execute(sql, args)
            return [dict(row) for row in cursor.fetchall()]


@pytest.fixture()
def remote():
    fake = FakeTurso()
    yield fake
    fake.close()


@pytest.fixture(autouse=True)
def _sync_env(monkeypatch, tmp_path, _isolate_user_data, engine):  # noqa: ANN001
    """每个用例：口令、同步表清空、设置文件里指向假 Turso 的配置。"""
    monkeypatch.setenv("QF_SYNC_PASSPHRASE", "test-passphrase")
    monkeypatch.setenv("QF_SYNC_KEY_FILE", str(tmp_path / "missing.key"))
    sync.ensure_sync_schema(engine)
    with engine.begin() as conn:
        for table in ("qf_sync_journal", "qf_sync_rows", "qf_sync_state"):
            conn.exec_driver_sql(f"DELETE FROM {table}")


def _configure(url: str, token: str = "test-token") -> None:
    settings_file.save({"turso": {"url": url, "token": token, "enabled": True}})


def _client(remote: FakeTurso) -> sync.TursoClient:
    return sync.TursoClient(remote.url, "test-token")


def _machine(tmp_path, name: str):  # noqa: ANN001, ANN202
    engine = create_engine(
        f"sqlite:///{tmp_path / name}.db", connect_args={"check_same_thread": False}
    )

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _record) -> None:  # noqa: ANN001
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    sync.ensure_sync_schema(engine)
    return engine


def _session(engine):  # noqa: ANN001, ANN202
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _count(engine, table: str) -> int:  # noqa: ANN001
    with engine.connect() as conn:
        return int(conn.exec_driver_sql(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])


def _titles(engine) -> list[str]:  # noqa: ANN001
    with engine.connect() as conn:
        return [row[0] for row in conn.exec_driver_sql("SELECT title FROM conversations ORDER BY title")]


def _contents(engine) -> list[str]:  # noqa: ANN001
    with engine.connect() as conn:
        return [row[0] for row in conn.exec_driver_sql("SELECT content FROM messages ORDER BY content")]


# ============================================================================ 用例


def test_url_normalization() -> None:
    assert sync.endpoint_for("libsql://qf-x.turso.io") == "https://qf-x.turso.io/v2/pipeline"
    assert sync.endpoint_for("https://qf-x.turso.io/") == "https://qf-x.turso.io/v2/pipeline"
    assert sync.endpoint_for("http://127.0.0.1:9") == "http://127.0.0.1:9/v2/pipeline"


def test_status_says_not_configured(engine) -> None:  # noqa: ANN001
    st = sync.status(engine)
    assert st["configured"] is False
    assert st["pending"] == 0
    assert "token" not in st


def test_push_and_pull_round_trip(engine, remote, tmp_path) -> None:  # noqa: ANN001
    session = _session(engine)
    conv = Conversation(title="A 的对话")
    session.add(conv)
    session.commit()
    msg = Message(conversation_id=conv.id, role="user", content="秘密正文 hello")
    session.add(msg)
    session.commit()
    session.close()

    _configure(remote.url)
    first = sync.run_once(engine, _client(remote))
    assert first["ok"] is True
    # 自己的行也会被中转站读回来（游标语义简单），但影子 at 相同 → 不重复落地
    assert first["applied"] == 0

    # 对话以密文出去：远端表里搜不到正文，且标记了 enc=1
    remote_rows = remote.rows("SELECT tbl, pk, enc, payload FROM qf_sync_rows")
    message_row = next(row for row in remote_rows if row["tbl"] == "messages")
    assert message_row["enc"] == 1
    assert "秘密正文" not in (message_row["payload"] or "")

    other = _machine(tmp_path, "other")
    sync.run_once(other, _client(remote))
    assert _titles(other) == ["A 的对话"]
    assert _contents(other) == ["秘密正文 hello"]
    # uid 是同步身份，必须原样过去；本地 id 由第二台机器自己发
    with other.connect() as conn:
        copied_uid = conn.exec_driver_sql("SELECT uid FROM messages").fetchone()[0]
    assert copied_uid == msg.uid
    # 拉下来的行不能被标成脏（触发器在应用期间静默）
    with other.connect() as conn:
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM qf_sync_journal").fetchone()[0] == 0


def test_memory_and_plan_doc_reach_a_fresh_machine(engine, remote, tmp_path) -> None:  # noqa: ANN001
    """全局记忆与计划书正文都是**用户数据**：云端有一份，新机器全量拉得到。

    这两样最容易漏 —— 它们不是对话那种"大表"，加功能时常常忘了同步。
    `plans.doc` 跟着 `plans` 整行走；`memories` 曾经漏在 `PULL_ORDER` 外，
    推送通、增量拉取也通，只有**新机器的首次全量拉取**永远读不到它。
    """
    session = _session(engine)
    memory.create(
        session,
        title="叫他 rd",
        body="回复用中文；他在准备数字芯片设计的面试",
        kind="profile",
    )
    session.add(Plan(title="两周计划", doc="# 计划书\n\n先读综述，再手推。\n"))
    session.commit()
    session.close()

    _configure(remote.url)
    sync.run_once(engine, _client(remote))

    # 两样都在 PRIVATE_TABLES 里：云端只有密文，搜不到正文
    remote_rows = remote.rows("SELECT tbl, enc, payload FROM qf_sync_rows")
    for tbl, secret in (("memories", "数字芯片"), ("plans", "先读综述")):
        row = next(one for one in remote_rows if one["tbl"] == tbl)
        assert row["enc"] == 1
        assert secret not in (row["payload"] or "")

    other = _machine(tmp_path, "other")
    sync.run_once(other, _client(remote))
    with other.connect() as conn:
        memories = conn.exec_driver_sql("SELECT title, body FROM memories").fetchall()
        docs = conn.exec_driver_sql("SELECT title, doc FROM plans").fetchall()
    assert [tuple(row) for row in memories] == [
        ("叫他 rd", "回复用中文；他在准备数字芯片设计的面试")
    ]
    assert [tuple(row) for row in docs] == [("两周计划", "# 计划书\n\n先读综述，再手推。\n")]


def test_pull_accepts_legacy_plaintext_rows_of_now_private_tables(engine, remote, tmp_path) -> None:  # noqa: ANN001
    """升级前上云的明文记忆行，升级后仍要拉得下来。

    把表划进 `PRIVATE_TABLES` 只改变**推送**时的行为；远端镜像里那些早期的
    明文行还在。拉取如果一律按"这张表必须解密"去读，升级当天同步就会炸。
    """
    pk = json.dumps(["m" * 32])
    payload = {
        "id": "m" * 32,
        "title": "旧记忆",
        "body": "升级前是明文上云的",
        "kind": "fact",
        "created_at": "2026-11-01T00:00:00+00:00",
        "updated_at": "2026-11-01T00:00:00+00:00",
    }
    _client(remote).ensure_schema()
    with remote.lock:
        remote.db.execute(
            "INSERT INTO qf_sync_rows(tbl, pk, at, origin, deleted, enc, payload)"
            " VALUES('memories', ?, ?, 'old-machine', 0, 0, ?)",
            (pk, time.time(), json.dumps(payload, ensure_ascii=False)),
        )

    other = _machine(tmp_path, "other")
    _configure(remote.url)
    sync.run_once(other, _client(remote))
    with other.connect() as conn:
        got = conn.exec_driver_sql("SELECT title, body FROM memories").fetchall()
    assert [tuple(row) for row in got] == [("旧记忆", "升级前是明文上云的")]

    # 这一轮拉完之后，rescope 把它排进了重推队列；下一轮推送就该换成密文
    sync.run_once(other, _client(remote))
    row = remote.rows("SELECT enc, payload FROM qf_sync_rows WHERE tbl = 'memories'")[0]
    assert row["enc"] == 1
    assert "升级前是明文上云的" not in (row["payload"] or "")


def test_full_pull_covers_every_sync_table() -> None:
    """`_full_pull` 只读 `PULL_ORDER` —— 它必须覆盖所有同步表。

    缺一张不会报错：那类数据只是**永远到不了新机器**。所以把它钉成断言，
    以后加表时漏了这里，测试当场红。
    """
    assert set(sync.PULL_ORDER) == set(sync.SYNC_TABLES)


def test_two_machines_new_messages_do_not_collide(engine, remote, tmp_path) -> None:  # noqa: ANN001
    """两台机器各自离线下写消息，本地 id 都会是 1 —— 同步后两条都得在。"""
    other = _machine(tmp_path, "other")

    for target, content in ((engine, "第一台"), (other, "第二台")):
        session = _session(target)
        conv = Conversation(title=content)
        session.add(conv)
        session.commit()
        session.add(Message(conversation_id=conv.id, role="user", content=content))
        session.commit()
        session.close()

    _configure(remote.url)
    sync.run_once(engine, _client(remote))
    sync.run_once(other, _client(remote))
    sync.run_once(engine, _client(remote))

    assert sorted(_contents(engine)) == ["第一台", "第二台"]
    assert sorted(_contents(other)) == ["第一台", "第二台"]
    with engine.connect() as conn:
        ids = [row[0] for row in conn.exec_driver_sql("SELECT id FROM messages ORDER BY id")]
    assert len(set(ids)) == 2


def test_last_write_wins_on_a_row(engine, remote, tmp_path) -> None:  # noqa: ANN001
    other = _machine(tmp_path, "other")
    session = _session(engine)
    conv = Conversation(title="旧标题")
    session.add(conv)
    session.commit()
    session.close()

    _configure(remote.url)
    sync.run_once(engine, _client(remote))
    sync.run_once(other, _client(remote))

    session = _session(other)
    row = session.get(Conversation, conv.id)
    row.title = "新标题"
    session.commit()
    session.close()
    sync.run_once(other, _client(remote))
    sync.run_once(engine, _client(remote))
    assert _titles(engine) == ["新标题"]


def test_delete_propagates(engine, remote, tmp_path) -> None:  # noqa: ANN001
    other = _machine(tmp_path, "other")
    session = _session(engine)
    conv = Conversation(title="要删的")
    session.add(conv)
    session.commit()
    session.add(Message(conversation_id=conv.id, role="user", content="跟它一起走"))
    session.commit()
    session.close()

    _configure(remote.url)
    sync.run_once(engine, _client(remote))
    sync.run_once(other, _client(remote))
    assert _count(other, "messages") == 1

    session = _session(engine)
    session.delete(session.get(Conversation, conv.id))
    session.commit()
    session.close()

    sync.run_once(engine, _client(remote))
    sync.run_once(other, _client(remote))
    assert _count(other, "conversations") == 0
    assert _count(other, "messages") == 0


def test_settings_secrets_never_leave_and_stay_local(engine, remote, tmp_path) -> None:  # noqa: ANN001
    """AI 密钥 / Turso 令牌不上传；拉回来的设置保留本机凭据与机器块。"""
    other = _machine(tmp_path, "other")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO app_settings(id, data, client_rev, updated_at)"
            " VALUES(1, ?, 0, datetime('now'))",
            (
                json.dumps(
                    {
                        "theme": "dark",
                        "ai": {"apiKey": "sk-test-very-secret", "model": "m1"},
                        "turso": {"url": "libsql://x", "token": "tok-secret"},
                    }
                ),
            ),
        )

    _configure(remote.url)
    sync.run_once(engine, _client(remote))
    payloads = [
        row["payload"]
        for row in remote.rows("SELECT payload FROM qf_sync_rows WHERE tbl='app_settings'")
    ]
    assert payloads
    assert "sk-test-very-secret" not in payloads[0]
    assert "tok-secret" not in payloads[0]
    assert "libsql://x" not in payloads[0]

    settings_file.save(
        {
            "theme": "light",
            "ai": {"apiKey": "other-test-key", "model": "old"},
            "turso": {"url": "libsql://other", "token": "other-token", "enabled": True},
        }
    )
    sync.run_once(other, _client(remote))
    saved = settings_file.load()
    assert saved["theme"] == "dark"                 # 数据面的设置跟远端
    assert saved["ai"]["apiKey"] == "other-test-key"     # 本机凭据留下
    assert saved["turso"]["token"] == "other-token"  # 连接块整个是本机的


def test_attempts_union_updates_counters_once(engine, remote, tmp_path) -> None:  # noqa: ANN001
    """两台机器各做一次同一道题：并集是两条流水，计数是 2（不能重复加）。"""
    other = _machine(tmp_path, "other")
    question = "q-1"

    def add_attempt(target, status: str) -> None:  # noqa: ANN001
        session = _session(target)
        when = datetime.now(UTC)
        session.add(
            Attempt(
                id=uuid.uuid4(),
                question_id=question,
                at=when,
                day=when.date(),
                status=status,
                score=1.0,
                topic_key="",
            )
        )
        session.add(
            Record(
                question_id=question,
                attempts=1,
                correct=1 if status == "correct" else 0,
                wrong=0 if status == "correct" else 1,
                first_at=int(when.timestamp() * 1000),
                last_at=int(when.timestamp() * 1000),
                last_status=status,
                last_score=1.0,
                patch={},
            )
        )
        session.commit()
        session.close()

    add_attempt(engine, "correct")
    add_attempt(other, "wrong")

    _configure(remote.url)
    sync.run_once(engine, _client(remote))
    sync.run_once(other, _client(remote))
    sync.run_once(engine, _client(remote))
    sync.run_once(other, _client(remote))

    for target in (engine, other):
        assert _count(target, "attempts") == 2
        with target.connect() as conn:
            row = conn.exec_driver_sql(
                "SELECT attempts, correct, wrong FROM records WHERE question_id = ?", (question,)
            ).fetchone()
        assert row[0] == 2, f"计数重复或漏了：{tuple(row)}"


def test_fresh_machine_counts_attempts_once(engine, remote, tmp_path) -> None:  # noqa: ANN001
    """新机器第一次全量：records 里带的计数**不能**再用流水加一遍。"""
    when = datetime.now(UTC)
    session = _session(engine)
    session.add(
        Attempt(
            id=uuid.uuid4(),
            question_id="q-fresh",
            at=when,
            day=when.date(),
            status="correct",
            score=1.0,
            topic_key="",
        )
    )
    session.add(
        Record(
            question_id="q-fresh",
            attempts=1,
            correct=1,
            wrong=0,
            first_at=int(when.timestamp() * 1000),
            last_at=int(when.timestamp() * 1000),
            last_status="correct",
            last_score=1.0,
            patch={},
        )
    )
    session.commit()
    session.close()

    _configure(remote.url)
    sync.run_once(engine, _client(remote))

    fresh = _machine(tmp_path, "fresh")
    sync.run_once(fresh, _client(remote))
    assert _count(fresh, "attempts") == 1
    with fresh.connect() as conn:
        row = conn.exec_driver_sql(
            "SELECT attempts, correct, wrong FROM records WHERE question_id = 'q-fresh'"
        ).fetchone()
    assert tuple(row) == (1, 1, 0)


def test_legacy_counter_without_attempt_survives(engine, remote, tmp_path) -> None:  # noqa: ANN001
    """老进度可能只有计数、没有流水（导入的），那份差额不能丢。"""
    session = _session(engine)
    session.add(
        Record(
            question_id="q-legacy",
            attempts=1,
            correct=0,
            partial=0,
            wrong=1,
            first_at=0,
            last_at=int(datetime.now(UTC).timestamp() * 1000),
            last_status="wrong",
            last_score=1.0,
            patch={},
        )
    )
    session.commit()
    session.close()

    _configure(remote.url)
    sync.run_once(engine, _client(remote))
    fresh = _machine(tmp_path, "fresh")
    sync.run_once(fresh, _client(remote))
    with fresh.connect() as conn:
        row = conn.exec_driver_sql(
            "SELECT attempts, correct, partial, wrong FROM records WHERE question_id = 'q-legacy'"
        ).fetchone()
    assert tuple(row) == (1, 0, 0, 1)
    assert _count(fresh, "attempts") == 0


def test_missing_passphrase_refuses_instead_of_uploading_plaintext(engine, remote, monkeypatch) -> None:  # noqa: ANN001
    session = _session(engine)
    conv = Conversation(title="不许明文")
    session.add(conv)
    session.commit()
    session.close()

    monkeypatch.delenv("QF_SYNC_PASSPHRASE", raising=False)
    monkeypatch.setenv("QF_SYNC_KEY_FILE", "/nonexistent/qf-sync.key")
    _configure(remote.url)
    with pytest.raises(sync.SyncError):
        sync.run_once(engine, _client(remote))

    assert remote.rows("SELECT * FROM qf_sync_rows") == []


def test_pull_without_key_when_remote_has_private_data(engine, remote, tmp_path, monkeypatch) -> None:  # noqa: ANN001
    other = _machine(tmp_path, "other")
    session = _session(other)
    conv = Conversation(title="远端加密")
    session.add(conv)
    session.commit()
    session.close()

    _configure(remote.url)
    sync.run_once(other, _client(remote))

    monkeypatch.delenv("QF_SYNC_PASSPHRASE", raising=False)
    monkeypatch.setenv("QF_SYNC_KEY_FILE", "/nonexistent/qf-sync.key")
    with pytest.raises(sync.SyncError):
        sync.run_once(engine, _client(remote))
    assert _count(engine, "conversations") == 0


def test_baseline_captures_rows_created_before_triggers(tmp_path) -> None:  # noqa: ANN001
    """触发器出生之前就有的行，也要被第一次同步带上。"""
    machine = _machine(tmp_path, "legacy")
    with machine.begin() as conn:
        # 模拟"老库"：先有行，再装同步结构
        conn.exec_driver_sql("DROP TRIGGER IF EXISTS qf_sync1_conversations_insert")
        conn.exec_driver_sql(
            "INSERT INTO conversations(id, title, pinned, archived, created_at, updated_at, folder)"
            " VALUES(?, '老对话', 0, 0, datetime('now'), datetime('now'), '')",
            (uuid.uuid4().hex,),
        )
    before = sync._baseline(machine)
    assert before == 1
    with machine.connect() as conn:
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM qf_sync_journal").fetchone()[0] == 1


def test_baseline_revisits_tables_that_gained_rows_without_shadows(tmp_path) -> None:  # noqa: ANN001
    """表晚于第一次同步才有行（或那批行没经过触发器）时，也要能补上。

    实测栽在这里：181 条日程与 9 条计划条目躺在本地，远端一行都没有，
    而每次同步都报成功 —— 老写法 `baseline_done=1` 一刀切，补漏只可能发生一次，
    且远早于这些表出生。判据是"本地行数 ≠ 影子行数"，与表是什么时候加的无关。
    """
    machine = _machine(tmp_path, "late")
    assert sync._baseline(machine) == 0, "空库：登记一遍，没有可补的行"

    session = _session(machine)
    session.execute(text("DROP TRIGGER IF EXISTS qf_sync1_schedule_events_insert"))
    start = datetime.now(UTC)
    session.add(
        ScheduleEvent(
            id="evt-late-1",
            title="自习",
            start_at=start,
            end_at=start + timedelta(hours=1),
            all_day=False,
        )
    )
    session.commit()
    session.close()

    assert sync._baseline(machine) == 1, "没有影子行的日程要被补进队列"
    with machine.connect() as conn:
        assert conn.exec_driver_sql(
            "SELECT COUNT(*) FROM qf_sync_journal WHERE tbl = 'schedule_events'"
        ).fetchone()[0] == 1

    assert sync._baseline(machine) == 0, "补过之后不该每次都重扫整表"


# ============================================================================ 资料整包


def _seed_material(session, slug: str, text: str) -> None:  # noqa: ANN001
    material = Material(
        slug=slug, subject="测试", title=slug, source_path="", sha256=slug, lines=1
    )
    session.add(material)
    session.flush()
    session.add(
        MaterialSlice(
            material_id=material.id,
            slice_id="s1",
            path=[],
            start_line=1,
            end_line=1,
            tokens="",
            figures=[],
            summary="",
            text=text,
            windows=0,
        )
    )
    session.commit()


def _drop_material(engine, slug: str) -> None:  # noqa: ANN001
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "DELETE FROM material_slices WHERE material_id IN"
            " (SELECT id FROM materials WHERE slug = ?)",
            (slug,),
        )
        conn.exec_driver_sql("DELETE FROM materials WHERE slug = ?", (slug,))


def test_library_pack_auto_sync_pushes_then_pulls(engine, remote, tmp_path) -> None:  # noqa: ANN001
    """资料包：本机有东西 → 推；另一台空机器 → 拉（含切片正文）。"""
    slug = "sync-test-material"
    session = _session(engine)
    _seed_material(session, slug, "第一段\n第二段")
    session.close()

    try:
        _configure(remote.url)
        out = sync.run_full(engine, _client(remote))
        assert out["library"]["action"] == "push"
        assert out["library"]["textSlices"] == 1

        # 什么都没变 → 不再传第二遍
        again = library_cloud.sync_pack(_session(engine), remote=_client(remote))
        assert again["action"] == "idle"

        # 另一台机器（空库）拉下来：材料与正文都在
        other = _machine(tmp_path, "other")
        other_session = _session(other)
        pulled = library_cloud.sync_pack(other_session, remote=_client(remote))
        assert pulled["action"] == "pull"
        material = other_session.query(Material).filter_by(slug=slug).one()
        slice_row = other_session.query(MaterialSlice).filter_by(material_id=material.id).one()
        assert slice_row.text == "第一段\n第二段"
        other_session.close()
    finally:
        _drop_material(engine, slug)


def test_library_pack_never_pushes_an_empty_pack(tmp_path, remote) -> None:  # noqa: ANN001
    """本机没有材料时绝不推空包 —— 那会把另一台机器的资料库抹掉。"""
    remote.db.execute(
        "CREATE TABLE qf_library (id INTEGER PRIMARY KEY, at REAL NOT NULL,"
        " origin TEXT NOT NULL, bytes INTEGER NOT NULL DEFAULT 0, payload TEXT)"
    )
    remote.db.execute(
        "CREATE TABLE qf_library_text (idx INTEGER PRIMARY KEY,"
        " bytes INTEGER NOT NULL DEFAULT 0, payload TEXT)"
    )
    remote.db.execute(
        "INSERT INTO qf_library(id, at, origin, bytes, payload) VALUES(1, 100.0, 'other', 2, '{}')"
    )
    # 用一台**全新**的机器，而不是共享的测试库：materials 属于 bank 表，
    # `_isolate_user_data` 不清它，别的用例会留下材料。
    empty = _machine(tmp_path, "empty")
    out = library_cloud.sync_pack(_session(empty), remote=_client(remote))
    assert out["action"] == "idle"
    assert "不覆盖云端" in out["reason"]
    assert remote.rows("SELECT payload FROM qf_library")[0]["payload"] == "{}"


def test_sync_status_exposes_auto_mode(client) -> None:  # noqa: ANN001
    """设置页要能看到自动同步的状态（测试环境里它是关的）。"""
    body = client.get("/api/sync/status").json()
    assert body["auto"]["enabled"] is False
    assert body["auto"]["started"] is False
    assert body["auto"]["interval"] >= 15
    assert autosync.start() is False
