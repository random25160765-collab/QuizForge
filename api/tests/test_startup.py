"""启动检查：**双击之后那份"能不能用"的清单**。

它在启动页上以进度条 + 逐条状态的形式出现，所以这里的判定要准：

* `fail` 会挡住"可以开始用"（数据目录写不进去、库结构不对、前端产物不在）；
* `warn` 只提示（题库空、密钥没配、运行时没取）—— 少一个能力不该把人拦在门外。

其中"库结构不对"那条是拿真实翻车换来的：`messages.id` 是 `BIGINT` 时
SQLite 不自增，界面能开、题能读、**一写就炸**。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app import startup


def test_report_shape(client) -> None:  # noqa: ANN001
    """/api/startup 要给出：能不能用、进度、以及逐条检查。"""
    body = client.get("/api/startup").json()
    assert set(body) >= {"ok", "progress", "steps"}
    assert 0.0 <= body["progress"] <= 1.0
    keys = {step["key"] for step in body["steps"]}
    assert {"data", "database", "frontend"} <= keys, "关键的几条不能少"
    for step in body["steps"]:
        assert step["status"] in {"ok", "warn", "fail"}
        assert step["label"], "每条都要有给人看的名字"


def test_all_green_on_a_healthy_install(client) -> None:  # noqa: ANN001
    """正常环境下不该有 fail（测试库是新建的、前端产物在）。"""
    body = client.get("/api/startup").json()
    failures = [step for step in body["steps"] if step["status"] == "fail"]
    assert not failures, f"不该有失败的检查：{failures}"
    assert body["ok"] is True


def test_a_broken_schema_is_a_hard_failure(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    """库结构不对 = **fail**（读得出、写不进，最坑的那种）。

    这条直接盯住 `messages.id` 那类问题：整数主键不是 `INTEGER` 就是不自增，
    启动页上必须红着说清楚，而不是等用户发消息时才报 IntegrityError。
    """
    path = tmp_path / "old.db"
    with sqlite3.connect(str(path)) as conn:
        conn.execute("CREATE TABLE messages (id BIGINT PRIMARY KEY, content TEXT)")

    from app import db as db_module
    from app.db import sqlite_rowid_pk_problems

    problems = sqlite_rowid_pk_problems(path)
    assert problems, "这种库必须被认出来"
    assert "messages.id" in problems[0]

    # 还要验「接线」：真的有问题时，启动检查那条要判成 fail（而不是只 warn）
    startup._schema_ok.cache_clear()  # noqa: SLF001
    monkeypatch.setattr(db_module, "_rowid_pk_problems", lambda conn: ["messages.id 声明成 BIGINT"])
    try:
        ok, detail = startup._schema_ok()  # noqa: SLF001
    finally:
        startup._schema_ok.cache_clear()  # noqa: SLF001
    assert ok is False
    assert "BIGINT" in detail


def test_missing_data_dir_is_reported(monkeypatch) -> None:  # noqa: ANN001
    """数据目录写不进去 → fail（那是"什么都存不下"）。"""

    class Boom(Path):
        def mkdir(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise OSError("权限不够")

    from app import config

    settings = config.get_settings()
    monkeypatch.setattr(type(settings), "data_dir", property(lambda self: Boom("/nope")), raising=False)
    step = startup._data_dir_step()
    assert step.status == "fail"
    assert "写不进去" in step.detail


def test_the_first_download_holds_the_startup_page(client, monkeypatch) -> None:  # noqa: ANN001
    """首启下载 146M 时启动页要停住、把进度画出来（用户 2026-11-10）。

    约定：`downloading` 为真 ⇒ `progress` 严格小于 1（启动页据此不跳转），
    而且进度按**文件数**折算 —— 清单里的字节数是打包时的近似值，按它画会先跑满再卡住。
    """
    from app import heavy_deps

    monkeypatch.setattr(heavy_deps, "progress", lambda: {
        "state": "running",
        "done": 0,
        "total": 0,
        "files": 3,
        "filesTotal": 10,
        "detail": "正在下载必要的依赖",
        "percent": 30,
    })
    monkeypatch.setattr(heavy_deps, "prefetching", lambda: True)

    body = client.get("/api/startup").json()
    assert body["downloading"] is True
    assert body["progress"] == 0.3
    heavy = next(step for step in body["steps"] if step["key"] == "heavy")
    assert heavy["status"] == "warn", "下载只挡住跳转，不是 fail"
    assert heavy["detail"] == "正在下载必要的依赖"


def test_a_settled_runtime_never_locks_people_out(client, monkeypatch) -> None:  # noqa: ANN001
    """取完 / 取不到都该放行：146M 是可选能力，不能把人锁在启动页外面。

    失败只把原因写进那一行（设置页那颗「重新下载并校验」还能补），
    进度照常到 100%，启动页该跳进主界面。
    """
    from app import heavy_deps

    monkeypatch.setattr(heavy_deps, "progress", lambda: {
        "state": "failed",
        "done": 0,
        "total": 0,
        "files": 0,
        "filesTotal": 10,
        "detail": "下载失败（pyodide.asm.wasm）—— timed out",
        "percent": 0,
    })
    monkeypatch.setattr(heavy_deps, "prefetching", lambda: False)
    monkeypatch.setattr(heavy_deps, "is_ready", lambda *args, **kwargs: False)

    body = client.get("/api/startup").json()
    assert body["downloading"] is False
    assert body["progress"] == 1.0
    heavy = next(step for step in body["steps"] if step["key"] == "heavy")
    assert heavy["status"] == "warn"
    assert "timed out" in heavy["detail"]
