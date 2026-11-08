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


class _Canceled(Exception):
    """任务被取消。"""


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
                db, path, on_stage=lambda stage: _mark(job_id, stage)
            )
        with _session() as db:
            job = db.get(LibraryJob, job_id)
            if job is None:
                return
            if job.status != "canceled":
                job.status = "done"
                job.stage = "done"
                job.material_slug = str(report.get("slug") or "")
                job.message = (
                    f"{report.get('slices', 0)} 片 · {report.get('lines', 0)} 行"
                    f" · {report.get('embedded', 0)} 条向量"
                )
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
