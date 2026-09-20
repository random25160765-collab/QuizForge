"""跑脚本时那个**会合点**（`app/runs.py`）与它的两条上报路。

来由是用户的一句话："让 agent 等命令返回了再说话。"

原先不等：`run_python` 立刻返回，模型就开始说话（"脚本已经交进去跑了，我下一轮
就能看到"），而输出要等下一轮才进上下文 —— 说的话和实际结果脱节，看着像在编。
现在那一轮停在 `runs.wait()` 上，页面跑完从 `/chat/runs/{runId}` 上报后放行。

这里钉四件事：等到、超时返回 None（**不是**空 dict —— 别把"没等到"当"跑成功了"）、
没人等时上报不报错、以及上报之后信箱要收掉（不留草）。
"""

import asyncio
import pathlib
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app import runs  # noqa: E402


def test_deliver_wakes_the_waiter():
    """正常一路：有人等、有人报 → 拿到那份输出。"""

    async def go():
        task = asyncio.create_task(runs.wait("r-1", timeout=5))
        await asyncio.sleep(0.05)
        assert runs.deliver("r-1", {"ok": True, "text": "1 + 1 = 2", "ms": 3}) is True
        return await task

    got = asyncio.run(go())
    # `images` 也跟着来（matplotlib 那种图，壳收走的）——没有图时就是空列表，
    # 由 `runs.deliver` 统一补上（见 `_clean_images`），所以这里显式写出来
    assert got == {"ok": True, "text": "1 + 1 = 2", "ms": 3, "images": []}


def test_timeout_returns_none_not_empty_dict():
    """没人报就是 `None`：调用方据此说"没等到"，而不是当成跑成功了。"""

    async def go():
        return await runs.wait("r-never", timeout=0.2)

    started = time.perf_counter()
    assert asyncio.run(go()) is None
    assert time.perf_counter() - started < 3, "超时该按给的秒数回来"


def test_deliver_without_waiter_is_quiet():
    """页面报得比这边早（或那一轮早结束了）→ 返回 False，不能抛。"""
    assert runs.deliver("r-nobody", {"ok": True, "text": "x"}) is False
    assert runs.deliver("", {"ok": True}) is False


def test_slot_is_cleared_after_wait():
    """等完就把信箱收掉：runId 是一次性的，留着只长草。"""

    async def go():
        task = asyncio.create_task(runs.wait("r-gone", timeout=5))
        await asyncio.sleep(0.05)
        runs.deliver("r-gone", {"ok": True, "text": "ok"})
        await task

    asyncio.run(go())
    assert "r-gone" not in runs.pending()


def test_wait_does_not_block_the_event_loop():
    """等待期间事件循环必须还能干活 —— 上报正是从**另一个请求**进来的，
    堵住循环就成了自己等自己（永远等不到）。"""

    async def go():
        ticked = []

        async def ticker():
            for _ in range(3):
                await asyncio.sleep(0.05)
                ticked.append(1)

        task = asyncio.create_task(runs.wait("r-alive", timeout=5))
        tick = asyncio.create_task(ticker())
        await tick
        runs.deliver("r-alive", {"ok": True, "text": "done"})
        await task
        return ticked

    assert len(asyncio.run(go())) == 3, "等在后台线程里，循环不该被堵住"


def test_wait_from_another_thread():
    """换线程上报也要能放行（将来多 worker / 线程池时别变成偶发的死等）。"""
    holder = {}

    def waiter():
        holder["got"] = asyncio.run(runs.wait("r-thread", timeout=5))

    thread = threading.Thread(target=waiter)
    thread.start()
    time.sleep(0.15)
    assert runs.deliver("r-thread", {"ok": False, "text": "boom"}) is True
    thread.join(timeout=3)
    assert holder.get("got") == {"ok": False, "text": "boom", "images": []}


def test_blocking_wait_is_woken_by_another_thread():
    """**生产里就是这个形状**：agent 循环那一线程阻塞着等，页面那条上报
    由另一个线程接进来（FastAPI 把 `def` 端点丢在线程池里跑）。

    这条要是红的，就意味着"等输出"会变成死等 —— 用户看到的就是一块永远停在
    "运行中…"的零件。
    """
    holder = {}

    def waiter():
        holder["got"] = runs.wait_blocking("r-block", timeout=5)

    thread = threading.Thread(target=waiter)
    thread.start()
    time.sleep(0.15)
    assert runs.deliver("r-block", {"ok": True, "text": "跑完了", "ms": 7}) is True
    thread.join(timeout=3)
    assert holder.get("got") == {"ok": True, "text": "跑完了", "ms": 7, "images": []}


def test_images_are_capped_and_bad_ones_dropped():
    """上报的图收干净：**最多三张**、单张不能无限大 —— 它们会被写进库里那行运行。

    另外钉住一条边界：图只走零件那条路（`run.images`），**不进模型上下文** ——
    喂给模型的那条工具结果只取 `text`（见 agent_loop 里那段拼接）。
    """
    big = "A" * (runs.MAX_IMAGE_CHARS + 1)
    got = runs._clean_images(["x1", big, "x2", "x3", "x4"])
    assert got == ["x1", "x2", "x3"], "超大那张丢掉，其余按上限三张截住"
    assert runs._clean_images(None) == []

    # 走一遍 deliver：信箱里那份 payload 也必须是收干净的
    runs._slot("r-img")  # 建一个信箱（有人正在等的状态）
    assert runs.deliver("r-img", {"ok": True, "text": "t", "ms": 1, "images": ["a", big, "b", "c", "d"]}) is True
    assert runs._slots["r-img"]["payload"]["images"] == ["a", "b", "c"]
