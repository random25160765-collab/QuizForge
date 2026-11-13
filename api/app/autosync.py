"""自动同步：不是定时推，而是**有变化才动**。

## 为什么需要它（用户 2026-11-09 的原话）

> 必须自动同步，而且是全部数据的自动同步。

在此之前同步只有三个手动入口：设置页按钮、`make sync`、CLI。结果是
"另一台机器写了东西，这台不点就永远看不到" —— 用户那边表现为
「我这边现在甚至看不到日程/任务/材料」。

## 一轮做什么

1. **行级同步**（`sync.run_once`）：所有用户表 —— 对话、分组、消息、附件、
   自出的题、答题流水、计划、条目、日程、睡眠、健康档位、门禁、重排快照、
   设置。即 `sync.SYNC_TABLES`，与手动同步完全同一条路径。
2. **资料整包**（`library_cloud.sync_pack`）：材料 + 切片正文 + 图。
   这三张表是流水线写的、主键会撞号，所以不走行级，按业务键整包对齐。
3. **题库整包**（`bank_cloud.sync_pack`）：题目 + 考纲 + 概念 / 点 / 边。
   同理，而且它让 APK 不必再内置 62MB 的题库快照。

三件事**互相独立**：任何一件失败不影响另外两件。各自的结果都记在
`status()` 里，设置页能看到。

## 节奏（2026-11-09 第二次定调："有修改就同步"）

* **本机改了** → 下一拍就推。每 `QF_SYNC_POLL` 秒（默认 30）醒一次，先查本地
  `qf_sync_journal` 有没有行（本地 SQLite 的带索引 `LIMIT 1`，微秒级），有就跑一轮。
* **别的设备改了** → 同一个节拍里问一次远端 `MAX(seq)` 是否大于本机游标；
  大了才跑。Turso 没有"服务端推给我"的通道，只能问。
* **兜底** → 每 `QF_SYNC_INTERVAL` 秒（默认 1800 = 30 分钟）跑一轮完整的，
  覆盖行级日志看不见的整包（材料指纹 / 题库指纹）。
* `QF_SYNC_AUTO=0` 可关掉（测试与离线环境用）；启动后先等
  `QF_SYNC_FIRST_DELAY` 秒（默认 5），避开应用启动时的建表/迁移。

**空转一拍的成本**：一次本地 `SELECT 1`，最多加一次远端单行查询 —— 都不传正文。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

from . import library_cloud, sync
from .db import get_engine, get_session_factory

log = logging.getLogger("quizforge.autosync")

_started = False
_start_lock = threading.Lock()
_state_lock = threading.Lock()
_last: dict[str, Any] = {}


def auto_enabled() -> bool:
    raw = os.environ.get("QF_SYNC_AUTO", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _seconds(name: str, default: float, floor: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    return max(floor, value)


def poll_seconds() -> float:
    """探测间隔：30 秒。

    原来是 5 秒。手机上那是**每 5 秒一次 HTTPS**，射频一直被叫醒 —— 待机掉电
    的主因（2026-11-11 用户报"移动端耗电"）。现在"刚改完就要同步"由页面下拉
    （`theme/runtime/pullsync.js`）负责，自动节拍只管兜底，没必要这么勤。
    """
    return _seconds("QF_SYNC_POLL", 30.0, 1.5)


def interval_seconds() -> float:
    """兜底整轮的间隔：只为了发现**整包**（材料 / 题库）的变化。

    30 分钟一轮（2026-11-13 用户定，原先是 5 分钟）。整包会走数据库事务 + 上传
    下载，是这条链上最重的一次请求；而整包本身变化很稀疏（导入材料、批量入库），
    迟到半小时完全可接受。安卓上这一轮会真的唤醒射频，所以宁可晚。

    要立刻看到整包变化，下拉同步与 `run_now` 都会顺带把它跑掉，不必等这一拍。
    """
    return _seconds("QF_SYNC_INTERVAL", 1800.0, 15.0)


def _first_delay() -> float:
    return _seconds("QF_SYNC_FIRST_DELAY", 5.0, 0.0)


def configured() -> bool:
    return sync.turso_enabled() and sync.turso_configured()


def _local_pending() -> bool:
    """本机有没有还没推上去的行。表还没建 / 库正忙时当作"没有"，下一拍再问。"""
    try:
        with get_engine().connect() as conn:
            return (
                conn.exec_driver_sql("SELECT 1 FROM qf_sync_journal LIMIT 1").fetchone()
                is not None
            )
    except Exception:  # noqa: BLE001 - 探测失败不该让节拍器死掉
        return False


def tick(reason: str = "tick", engine: Any = None) -> dict:
    """跑一轮：行级同步 + 资料/题库整包。返回这一轮的结果（也给状态页看）。

    `engine` 只给测试注入；线上走 `get_engine()`（传 None）。
    """
    started = time.time()
    out: dict[str, Any] = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "reason": reason,
    }

    if not configured():
        out["skipped"] = "还没配置 Turso，或同步开关是关的"
    elif sync.is_running():
        out["skipped"] = "已有一轮同步在跑"
    else:
        try:
            # run_full = 行级表 + 资料整包 + 题库整包；有现成的一轮就复用它。
            out["sync"] = sync.run_full(engine)
        except Exception as exc:  # noqa: BLE001 - 后台节拍不能死
            log.warning("自动同步（行级）失败：%s", exc)
            out["sync"] = {"ok": False, "error": str(exc)}
            # 行级失败不拖累两个整包：分别再试一次。
            # 资料包先跑（题库的点靠 `materials.slug` 落地），与 run_full 的顺序一致。
            try:
                with get_session_factory()() as db:
                    out["library"] = library_cloud.sync_pack(db)
            except Exception as pack_exc:  # noqa: BLE001
                log.warning("自动同步（资料包）失败：%s", pack_exc)
                out["library"] = {"ok": False, "error": str(pack_exc)}
            try:
                from . import bank_cloud  # noqa: PLC0415 - 反过来 import sync

                with get_session_factory()() as db:
                    out["bank"] = bank_cloud.sync_pack(db)
            except Exception as bank_exc:  # noqa: BLE001
                log.warning("自动同步（题库包）失败：%s", bank_exc)
                out["bank"] = {"ok": False, "error": str(bank_exc)}

    out["seconds"] = round(time.time() - started, 2)
    global _last
    with _state_lock:
        _last = out
    return out


def _loop(first_delay: float) -> None:
    time.sleep(first_delay)
    last_full = 0.0
    while True:
        reason = ""
        wait = poll_seconds()
        try:
            if _local_pending():
                reason = "local"
            elif sync.remote_changed().get("changed"):
                reason = "remote"
            elif time.monotonic() - last_full >= interval_seconds():
                reason = "tick"
            if reason:
                tick(reason)
                last_full = time.monotonic()
        except Exception:  # noqa: BLE001 - 循环本身不能退出
            # 失败不要每 5 秒重试（会把日志刷满）：退到 15 秒，下一拍再来。
            wait = max(15.0, wait)
            last_full = time.monotonic()
            log.warning("自动同步这一拍失败（%.0f 秒后重试）", wait, exc_info=True)
        time.sleep(wait)


def start() -> bool:
    """启动后台节拍。已经在跑 / 测试关掉 / 没配置 —— 都返回 False。"""
    global _started
    with _start_lock:
        if _started:
            return False
        if not auto_enabled():
            log.info("自动同步未启动：QF_SYNC_AUTO=0")
            return False
        if not configured():
            log.info("自动同步未启动：Turso 未配置或同步开关关闭")
            return False
        _started = True

    threading.Thread(
        target=_loop, args=(_first_delay(),), name="qf-autosync", daemon=True
    ).start()
    log.info(
        "自动同步已启动：有改动就推（每 %.0f 秒探测），每 %.0f 秒兜底一轮",
        poll_seconds(),
        interval_seconds(),
    )
    return True


def started() -> bool:
    return _started


def status() -> dict:
    with _state_lock:
        last = dict(_last) if _last else None
    return {
        "started": _started,
        "enabled": auto_enabled(),
        "configured": configured(),
        "poll": poll_seconds(),
        "interval": interval_seconds(),
        "last": last,
    }


__all__ = [
    "auto_enabled",
    "configured",
    "interval_seconds",
    "poll_seconds",
    "start",
    "started",
    "status",
    "tick",
]
