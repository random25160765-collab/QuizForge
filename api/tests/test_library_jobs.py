"""资料流水线的后台队列：进程一走，任务不能留在半路上。

这一组的来由是用户 2026-11-13 报的那次：87MB 的 PDF 走到"向量"时 WSL 被关机，
再起来页面上那条一直显示"运行中 / 向量"，队列纹丝不动 —— 只有手点"重试"才动。
根因不在解析，也不在向量模型，而在**状态机没有主人**：`_next_job_id` 只捡
`queued`，而 worker 只在 `enqueue / retry / resume` 里才被拉起来。
"""

from __future__ import annotations

from app import jobs
from app.db import get_session_factory
from app.models import LibraryJob


def _add(status: str, stage: str = "embed") -> int:
    session = get_session_factory()()
    try:
        job = LibraryJob(
            rel="x.pdf", path="/tmp/x.pdf", status=status, stage=stage, message="向量"
        )
        session.add(job)
        session.commit()
        return int(job.id)
    finally:
        session.close()


def _drop(*ids: int) -> None:
    """`library_jobs` 是 derived 表，不在逐用例的清理名单里，只能自己收尾。"""
    session = get_session_factory()()
    try:
        for job_id in ids:
            row = session.get(LibraryJob, job_id)
            if row is not None:
                session.delete(row)
        session.commit()
    finally:
        session.close()


def _read(job_id: int) -> tuple[str, str]:
    session = get_session_factory()()
    try:
        job = session.get(LibraryJob, job_id)
        return (job.status, job.stage) if job is not None else ("", "")
    finally:
        session.close()


def test_a_job_left_running_is_requeued_and_the_worker_comes_back(client, monkeypatch) -> None:  # noqa: ANN001
    """孤儿任务回队列，并且要把 worker 拉起来 —— 只改状态不拉人是没人跑的。"""
    started: list[int] = []
    monkeypatch.setattr(jobs, "_ensure_worker", lambda: started.append(1))
    job_id = _add("running")
    try:
        out = jobs.recover()
        assert out["requeued"] == [job_id]
        assert _read(job_id) == ("queued", ""), "阶段要清空：重跑是从解析开始的"
        assert started == [1]
    finally:
        _drop(job_id)


def test_leftover_queued_jobs_get_a_worker_too(client, monkeypatch) -> None:  # noqa: ANN001
    """排队中的也要拉人：拖十个文件、跑第二个时关机，剩下九个重启后没人捡。"""
    started: list[int] = []
    monkeypatch.setattr(jobs, "_ensure_worker", lambda: started.append(1))
    job_id = _add("queued", stage="")
    try:
        out = jobs.recover()
        assert out["requeued"] == [], "排队中的不该被改写成别的状态"
        assert _read(job_id) == ("queued", "")
        assert started == [1]
    finally:
        _drop(job_id)


def test_finished_or_failed_jobs_are_left_alone(client, monkeypatch) -> None:  # noqa: ANN001
    """已经结束的不重跑：失败的那条要人看过再说，成功的不该被开机又算一遍。"""
    started: list[int] = []
    monkeypatch.setattr(jobs, "_ensure_worker", lambda: started.append(1))
    done_id = _add("done", stage="done")
    failed_id = _add("failed", stage="slice")
    try:
        out = jobs.recover()
        assert done_id not in out["requeued"]
        assert failed_id not in out["requeued"]
        assert _read(done_id) == ("done", "done")
        assert _read(failed_id) == ("failed", "slice")
        assert started == [], "没有活就别拉起 worker"
    finally:
        _drop(done_id, failed_id)
