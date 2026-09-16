"""任务表：SQLite + 租约 + 幂等 + 账本（pipeline.md §6）。

为什么不上 Celery / RQ：我们只需要「认领—续租—完成」，不需要 broker；
SQLite 的 WAL + BEGIN IMMEDIATE 足够，而且可查、可 diff、零运维。

三条机制：
  * 幂等：UNIQUE(kind, input_hash) + INSERT OR IGNORE，重跑只补缺口
  * 租约：认领时写 lease_until；worker 崩了租约过期自动回收，不会卡死
  * 账本：每个任务的 tokens / 耗时 / 成败落表，成本才有据可查
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id          TEXT PRIMARY KEY,
  kind        TEXT NOT NULL,
  input_hash  TEXT NOT NULL,
  payload     TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'todo',   -- todo | running | done | failed | dead
  attempts    INTEGER NOT NULL DEFAULT 0,
  lease_until REAL,
  worker      TEXT,
  result_path TEXT,
  error       TEXT,
  tokens_in   INTEGER NOT NULL DEFAULT 0,
  tokens_out  INTEGER NOT NULL DEFAULT 0,
  created_at  REAL NOT NULL,
  started_at  REAL,
  finished_at REAL,
  UNIQUE (kind, input_hash)
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks (status, created_at);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or config.DB_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    return conn


# ----------------------------------------------------------------- 派工

def add_task(conn, kind: str, input_hash: str, payload: dict) -> bool:
    """幂等入队。返回「这次是否真的变成了待跑」。

    撞到既有行分两种情况，必须区别对待：

    * `todo / running / done / failed / dead` —— 原样不动（幂等：同一份输入不重复派工）；
    * `archived` —— **复活**。归档的语义是「这一轮不跑」，重新派工就该重新跑。

    之前一律用 `INSERT OR IGNORE`：归档过的任务再也派不回来，命令回显「已存在 N 个」、
    队列却是空的 —— 看起来像 worker 卡住，实则是唯一键把活挡住了。
    """
    task_id = f"{kind}:{input_hash}"
    cur = conn.execute(
        "INSERT INTO tasks (id, kind, input_hash, payload, created_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT (kind, input_hash) DO UPDATE SET "
        "  status='todo', payload=excluded.payload, attempts=0, error=NULL, lease_until=NULL "
        "WHERE tasks.status='archived'",
        (task_id, kind, input_hash, json.dumps(payload, ensure_ascii=False), time.time()),
    )
    return cur.rowcount > 0


def requeue_dead(conn, kind: str | None = None) -> int:
    """把 dead 打回 todo（重跑时手动调用）。"""
    sql = "UPDATE tasks SET status='todo', attempts=0, error=NULL WHERE status='dead'"
    args: list = []
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    return conn.execute(sql, args).rowcount


# ----------------------------------------------------------------- 认领

def claim(conn, worker: str, lease_s: float, kind: str | None = None):
    """原子认领一个任务，顺带回收过期租约。没有可领的返回 None。"""
    now = time.time()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE tasks SET status='todo', lease_until=NULL, worker=NULL "
            "WHERE status='running' AND lease_until < ?",
            (now,),
        )
        sql = "SELECT id, kind, payload, attempts FROM tasks WHERE status='todo'"
        args: list = []
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        # 认领顺序：**先短后长**。以前是纯 created_at —— 重出任务（老）会一直插队，
        # 把成百上千个便宜又关键的校验任务饿在队尾（实测 64 个 worker 全在跑出题，
        # 校验队列一动不动）。按「校验→读图→抽取→出题」排，链路才走得动。
        sql += (
            " ORDER BY CASE kind WHEN 'verify' THEN 0 WHEN 'vision' THEN 1"
            " WHEN 'extract' THEN 2 ELSE 3 END, created_at LIMIT 1"
        )
        row = conn.execute(sql, args).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        conn.execute(
            "UPDATE tasks SET status='running', worker=?, lease_until=?, "
            "attempts=attempts+1, started_at=? WHERE id=?",
            (worker, now + lease_s, now, row["id"]),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"id": row["id"], "kind": row["kind"], "payload": json.loads(row["payload"])}


def renew(conn, task_id: str, lease_s: float) -> None:
    conn.execute(
        "UPDATE tasks SET lease_until=? WHERE id=?", (time.time() + lease_s, task_id)
    )


# ----------------------------------------------------------------- 收工

def finish(conn, task_id: str, result_path: str, tokens_in: int = 0, tokens_out: int = 0) -> None:
    conn.execute(
        "UPDATE tasks SET status='done', result_path=?, error=NULL, finished_at=?, "
        "tokens_in=?, tokens_out=?, lease_until=NULL WHERE id=?",
        (result_path, time.time(), tokens_in, tokens_out, task_id),
    )


def fail(conn, task_id: str, error: str, max_attempts: int) -> str:
    """失败：未超限打回 todo，超限进 dead。返回新状态。"""
    row = conn.execute("SELECT attempts FROM tasks WHERE id=?", (task_id,)).fetchone()
    attempts = row["attempts"] if row else max_attempts
    status = "todo" if attempts < max_attempts else "dead"
    conn.execute(
        "UPDATE tasks SET status=?, error=?, lease_until=NULL, finished_at=? WHERE id=?",
        (status, error[:2000], time.time(), task_id),
    )
    return status


# ----------------------------------------------------------------- 查询

def summary(conn) -> dict:
    rows = conn.execute(
        "SELECT kind, status, COUNT(*) AS n, SUM(tokens_in) AS tin, SUM(tokens_out) AS tout "
        "FROM tasks GROUP BY kind, status ORDER BY kind, status"
    ).fetchall()
    out: dict = {"by_kind": {}, "tokens_in": 0, "tokens_out": 0}
    for r in rows:
        out["by_kind"].setdefault(r["kind"], {})[r["status"]] = r["n"]
        out["tokens_in"] += r["tin"] or 0
        out["tokens_out"] += r["tout"] or 0
    return out


def spend(conn, rate_in: float, rate_out: float) -> tuple[float, int, int]:
    """账本累计花费（元）与 token。

    **这是唯一的成本真值**：任务表里每个真跑过的任务都记了 tokens。
    成本闸门靠它，而不是靠"感觉差不多"。注意删任务会让它变小 —— 所以任务只归档、不删。
    """
    row = conn.execute(
        "SELECT COALESCE(SUM(tokens_in), 0) AS i, COALESCE(SUM(tokens_out), 0) AS o FROM tasks"
    ).fetchone()
    return (row["i"] / 1e6 * rate_in + row["o"] / 1e6 * rate_out, row["i"], row["o"])


def pending(conn, kind: str | None = None) -> int:
    sql = "SELECT COUNT(*) AS n FROM tasks WHERE status IN ('todo','running')"
    args: list = []
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    return conn.execute(sql, args).fetchone()["n"]


def failures(conn, limit: int = 10) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, kind, attempts, error FROM tasks WHERE status IN ('failed','dead') "
        "ORDER BY finished_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
