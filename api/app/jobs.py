"""资料流水线的后台执行器：队列、暂停、取消、继续。

一个文件 = 一条 `LibraryJob`，状态机：

    queued ──► running ──► done
                │  ▲
       暂停 ────┘  └──── 继续
                │
       取消 ──► canceled     失败 ──► failed（可重试 → 回到 queued）

跑的是 `ingest.ingest_file`（解析 → 切片 → 正文 → 向量），阶段边界会回调，
所以页面能看到"卡在哪一步"；暂停/取消都在**阶段边界**生效 —— 一条切片正在写的时候
不会被打断，避免半个材料。

为什么单线程：解析（`pdftotext`）与向量化都是 CPU 活，并发只会互相抢；队列本身很短
（一次拖十来个文件），串行反而更快、也更容易说清进度。
"""

from __future__ import annotations

import threading
import time
from typing import Any

from sqlalchemy import select

from .db import get_session_factory
from .models import LibraryJob

#: 阶段的中文名（页面与提示共用一份，免得两边写法漂）
STAGE_LABELS: dict[str, str] = {
    "": "排队中",
    "parse": "解析",
    "slice": "切片",
    "embed": "向量",
    "done": "完成",
}

_lock = threading.Lock()
_paused = False
_thread: threading.Thread | None = None
_running_id: int | None = None


def _session() -> Any:
    return get_session_factory()()


def _ensure_worker() -> None:
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _thread = threading.Thread(target=_loop, name="qf-library-jobs", daemon=True)
        _thread.start()


def enqueue(db: Any, path: str, *, rel: str = "", size: int = 0) -> dict:
    """把一个文件排进队列（同一路径已有未完成任务时不重复排）。"""
    path = str(path)
    existing = db.scalar(
        select(LibraryJob)
        .where(LibraryJob.path == path, LibraryJob.status.in_(("queued", "running", "paused")))
        .limit(1)
    )
    if existing is not None:
        return _as_dict(existing, note="already queued")

    job = LibraryJob(rel=rel or path.rsplit("/", 1)[-1], path=path, status="queued", bytes=size)
    db.add(job)
    db.commit()
    db.refresh(job)
    _ensure_worker()
    return _as_dict(job)


def _as_dict(job: LibraryJob, note: str = "") -> dict:
    out = {
        "id": job.id,
        "rel": job.rel,
        "status": job.status,
        "stage": job.stage,
        "stageLabel": STAGE_LABELS.get(job.stage, job.stage or ""),
        "message": job.message,
        "material": job.material_slug,
        "bytes": job.bytes,
        "at": job.updated_at.isoformat() if job.updated_at else "",
    }
    if note:
        out["note"] = note
    return out


def list_jobs(db: Any, limit: int = 60) -> dict:
    rows = db.scalars(select(LibraryJob).order_by(LibraryJob.id.desc()).limit(limit)).all()
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.status] = counts.get(row.status, 0) + 1
    return {
        "paused": _paused,
        "running": _running_id,
        "counts": counts,
        "jobs": [_as_dict(row) for row in rows],
    }


def pause() -> dict:
    global _paused
    _paused = True
    return {"paused": True}


def resume() -> dict:
    global _paused
    _paused = False
    _ensure_worker()
    return {"paused": False}


def cancel(db: Any, job_id: int) -> dict:
    """取消一条：排队中的直接标记；正在跑的等它到下一个阶段边界。"""
    job = db.get(LibraryJob, job_id)
    if job is None:
        return {"ok": False, "error": "没有这个任务"}
    if job.status == "done":
        return {"ok": False, "error": "已经完成了"}
    job.status = "canceled"
    job.message = "已取消"
    db.commit()
    return {"ok": True, "id": job_id}


def retry(db: Any, job_id: int) -> dict:
    job = db.get(LibraryJob, job_id)
    if job is None:
        return {"ok": False, "error": "没有这个任务"}
    job.status = "queued"
    job.stage = ""
    job.message = ""
    db.commit()
    _ensure_worker()
    return {"ok": True, "id": job_id}


def recover() -> dict:
    """启动时收尸：上个进程没跑完的、还在排队的，重新交给 worker。

    进程一死，`running` 这个状态就没有主人了 —— `_next_job_id` 只捡 `queued`，
    而页面拿到的那一条会永远停在"运行中 / 向量"。WSL 关机、Ctrl-C、`--reload`
    都会留下这种孤儿；`queued` 的也一起僵住，因为 worker 只在 `enqueue/retry/
    resume` 里才被拉起来，重启之后没人拉。两件事合起来，用户的体感就是
    "流水线卡住了，非手点重试不动"。

    重跑是安全的：`ingest.ingest_file` 幂等（同一份文件重来一遍是覆盖切片）。
    丢掉的是上次的进度（解析、切片、向量都要重算），这里不做断点续跑 ——
    那要按材料存中间态，与"文件是唯一事实来源"的取舍相抵。
    """
    with _session() as db:
        stale = db.scalars(select(LibraryJob).where(LibraryJob.status == "running")).all()
        requeued = [row.id for row in stale]
        for row in stale:
            row.status = "queued"
            row.stage = ""
            row.message = "上次退出时没跑完，已重新排队"
        db.commit()
        waiting = db.scalar(select(LibraryJob.id).where(LibraryJob.status == "queued").limit(1))
    if waiting is not None:
        _ensure_worker()
    return {"requeued": requeued, "started": waiting is not None}


def _next_job_id() -> int | None:
    with _session() as db:
        job = db.scalar(
            select(LibraryJob).where(LibraryJob.status == "queued").order_by(LibraryJob.id).limit(1)
        )
        return job.id if job is not None else None


def _mark(job_id: int, stage: str) -> None:
    """阶段边界：更新进度；顺便检查"被取消/被暂停"。

    取消 → 抛 `_Canceled` 让这次任务结束；暂停 → 一直等到恢复（不抛，任务继续）。
    """
    while True:
        with _session() as db:
            job = db.get(LibraryJob, job_id)
            if job is None:
                raise _Canceled()
            if job.status == "canceled":
                raise _Canceled()
            job.stage = stage
            job.message = STAGE_LABELS.get(stage, stage)
            db.commit()
        if not _paused:
            return
        # 暂停：停在阶段边界，等恢复（恢复后 status 仍是 running）
        time.sleep(0.4)


def _progress(job_id: int) -> Any:
    """长活的进度回调：把"做到第几页 / 第几片"写进 `message`。

    为什么要有：OCR 527 页要二十几分钟、向量 19740 片要近两小时，页面只有
    一个阶段名，和卡死没有区别 —— 用户 2026-11-13 的原话就是"阶段不推进"。
    节流到 1.5 秒一次：每页都写的话，一个进度数字自己就成了写库热点。

    顺带在这里认取消：**这是长活里唯一稳定的检查点**（阶段边界隔着几十分钟）。
    发现任务被取消就抛 `_Canceled`，让它立刻停下 —— 否则用户点了取消，
    机器还要空转半小时。
    """
    last = [0.0]

    def report(label: str, done: int, total: int) -> None:
        now = time.monotonic()
        if now - last[0] < 1.5 and (not total or done < total):
            return
        last[0] = now
        try:
            with _session() as db:
                job = db.get(LibraryJob, job_id)
                if job is None:
                    raise _Canceled()
                if job.status == "canceled":
                    raise _Canceled()
                if job.status != "running":
                    return
                job.message = f"{label} {done}/{total}" if total else f"{label} {done}"
                db.commit()
        except _Canceled:
            raise
        except Exception:  # noqa: BLE001 - 写不进进度不能拖垮任务本身
            pass

    return report


class _Canceled(Exception):
    """任务被取消。

    `cancelled` 这个标记是给 `ingest` 用的：它把向量化整段包在
    `except Exception` 里（向量是可选的），没有标记的话连"取消"也会被当成
    "向量算失败"吞掉，任务于是继续跑到自然结束。
    """

    cancelled = True


def _loop() -> None:
    global _running_id
    while True:
        try:
            if _paused:
                time.sleep(0.5)
                continue
            job_id = _next_job_id()
            if job_id is None:
                time.sleep(0.8)
                continue
            _run(job_id)
        except Exception:  # noqa: BLE001 - 后台线程不能死
            time.sleep(1.0)


def _run(job_id: int) -> None:
    global _running_id
    from . import ingest  # noqa: PLC0415 - 避免顶层循环导入

    with _session() as db:
        job = db.get(LibraryJob, job_id)
        if job is None or job.status != "queued":
            return
        job.status = "running"
        job.stage = ""
        job.message = "开始"
        db.commit()
        path = job.path
    _running_id = job_id

    try:
        with _session() as db:
            report = ingest.ingest_file(
                db,
                path,
                on_stage=lambda stage: _mark(job_id, stage),
                on_progress=_progress(job_id),
            )
        with _session() as db:
            job = db.get(LibraryJob, job_id)
            if job is None:
                return
            if job.status != "canceled":
                job.status = "done"
                job.stage = "done"
                job.material_slug = str(report.get("slug") or "")
                pieces = int(report.get("slices") or 0)
                embedded = int(report.get("embedded") or 0)
                job.message = (
                    f"{pieces} 片 · {report.get('lines', 0)} 行 · {embedded} 条向量"
                )
                # 只算到一半要说出来：不然页面写着"完成"，检索却查不到后半份 ——
                # 用户没法从这里判断该不该再跑一次。
                if 0 < embedded < pieces:
                    job.message += f"（向量中断于 {embedded}/{pieces}，见日志）"
            db.commit()
    except _Canceled:
        with _session() as db:
            job = db.get(LibraryJob, job_id)
            if job is not None:
                job.status = "canceled"
                job.message = "已取消"
                db.commit()
    except Exception as exc:  # noqa: BLE001 - 单份失败只影响它自己
        with _session() as db:
            job = db.get(LibraryJob, job_id)
            if job is not None and job.status != "canceled":
                job.status = "failed"
                job.message = str(exc)[:400]
                db.commit()
    finally:
        _running_id = None
