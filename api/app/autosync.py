"""自动同步：把「推 / 拉」从按钮变成后台节拍。

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

两件事**互相独立**：行级失败不影响资料包，资料包失败也不影响行级。
各自的结果都记在 `status()` 里，设置页能看到。

## 条件与节奏

* 只有 `turso.enabled` 且 url/token 都填了才启动；
* 默认 **120 秒**一轮（`QF_SYNC_INTERVAL` 可改，下限 15 秒）；
* `QF_SYNC_AUTO=0` 可关掉（测试与离线环境用）；
* 启动后先等 `QF_SYNC_FIRST_DELAY` 秒（默认 5）再跑第一轮，避开
  应用启动时的建表/迁移，别和它抢 SQLite 的写锁。

**为什么敢跑这么勤**：什么都没变的时候，行级只查一次 `qf_sync_journal`，
资料包只读云端一行元数据再比指纹 —— 都不传正文。真正传数据只发生在
"本机或云端确实变了"之后。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

from . import library_cloud, sync
from .db import get_session_factory

log = logging.getLogger("quizforge.autosync")

_started = False
_start_lock = threading.Lock()
_state_lock = threading.Lock()
_last: dict[str, Any] = {}


def auto_enabled() -> bool:
    raw = os.environ.get("QF_SYNC_AUTO", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def interval_seconds() -> float:
    try:
        value = float(os.environ.get("QF_SYNC_INTERVAL", "120"))
    except ValueError:
        value = 120.0
    return max(15.0, value)


def _first_delay() -> float:
    try:
        return max(0.0, float(os.environ.get("QF_SYNC_FIRST_DELAY", "5")))
    except ValueError:
        return 5.0


def configured() -> bool:
    return sync.turso_enabled() and sync.turso_configured()


def tick() -> dict:
    """跑一轮：行级同步 + 资料整包。返回这一轮的结果（也给状态页看）。"""
    started = time.time()
    out: dict[str, Any] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}

    if not configured():
        out["skipped"] = "还没配置 Turso，或同步开关是关的"
    elif sync.is_running():
        out["skipped"] = "已有一轮同步在跑"
    else:
        try:
            # run_full = 行级表 + 资料整包；有现成的一轮就复用它。
            out["sync"] = sync.run_full()
        except Exception as exc:  # noqa: BLE001 - 后台节拍不能死
            log.warning("自动同步（行级）失败：%s", exc)
            out["sync"] = {"ok": False, "error": str(exc)}
            # 行级失败不拖累资料包：再单独试一次。
            try:
                with get_session_factory()() as db:
                    out["library"] = library_cloud.sync_pack(db)
            except Exception as pack_exc:  # noqa: BLE001
                log.warning("自动同步（资料包）失败：%s", pack_exc)
                out["library"] = {"ok": False, "error": str(pack_exc)}

    out["seconds"] = round(time.time() - started, 2)
    global _last
    with _state_lock:
        _last = out
    return out


def _loop(first_delay: float) -> None:
    time.sleep(first_delay)
    while True:
        try:
            tick()
        except Exception:  # noqa: BLE001 - 循环本身不能退出
            log.exception("自动同步轮次异常")
        time.sleep(interval_seconds())


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
    log.info("自动同步已启动：每 %.0f 秒一轮", interval_seconds())
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
        "interval": interval_seconds(),
        "last": last,
    }


__all__ = ["auto_enabled", "configured", "interval_seconds", "start", "started",
           "status", "tick"]
