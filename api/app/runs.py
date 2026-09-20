"""跑脚本时的**会合点**：零件发出去之后，停下来等页面把输出报回来。

为什么非要它：`run_python` 的代码是在**页面**的沙箱（Pyodide）里跑的，服务端只负责
把零件发出去 —— "这一轮到底跑出了什么"只有页面知道。用户的要求是：

> 让 agent 等命令返回了再说话。

原先不等：工具立刻返回，模型就开始说话（"脚本已经交进去跑了，我下一轮就能看到"），
输出则要等下一轮才进上下文 —— 于是它说的话和实际结果脱节，看起来就像在编。

会合怎么做：`run_python` 的那一轮在 `wait()` 上停住（异步、不堵事件循环），
页面跑完 `POST /chat/runs/{runId}` → `deliver()` 放行，输出当场并进工具结果。

没人来报（页面没开、脚本死循环、用户把页面关了）就**超时认输**并如实说没等到 ——
比"一直挂着"强，也比编一个结果强。

用 `threading` 而不是 `asyncio.Event`：上报是从**另一个请求**进来的，虽然本机单进程
下通常是同一个事件循环，但 threading 的事件跨线程/跨循环都成立，不会因为将来换了
部署方式（多 worker / 线程池）变成偶发的死等。
"""

from __future__ import annotations

import asyncio
import threading
import time

#: 等页面报输出最多等多久。
#:
#: 45 → 90：45 秒不够用了 —— 实测撞到过一次"脚本画两张图、还扫了一轮 n 到 1024"，
#: 跑到一半就过了时限，模型那边收到的是"没等到沙箱的输出"（而面板上其实出了图）。
#: 首次启动运行时（本机缓存热时 2~3 秒，冷的时候要下载）也吃这段预算。
#: 宁可多等一会儿，也不要在一个马上要跑完的脚本上认输。
DEFAULT_TIMEOUT = 90.0

_lock = threading.Lock()
_slots: dict[str, dict] = {}


def _slot(run_id: str) -> dict:
    """拿这个 runId 的信箱（没有就建一个）。"""
    with _lock:
        found = _slots.get(run_id)
        if found is None:
            found = {"event": threading.Event(), "payload": None, "at": time.time()}
            _slots[run_id] = found
        return found


def wait_blocking(run_id: str, timeout: float = DEFAULT_TIMEOUT) -> dict | None:
    """等这次运行的输出（**同步**版，agent 循环用的就是它）。

    为什么同步等待在这里是安全的：这个应用是同步的（`def` 端点，FastAPI 丢进
    线程池跑），agent 循环本身是普通生成器。这一等只占住**这一条请求**的线程，
    而页面那条 `/chat/runs/{runId}` 上报是**另一个线程**接的 —— 所以放行不会被
    自己挡住。（要是哪天把它改成事件循环里的协程，必须换成 `await wait()`，
    否则就是自己等自己。）
    """
    if not run_id:
        return None
    slot = _slot(run_id)
    try:
        if not slot["event"].wait(float(timeout)):
            return None
        return slot["payload"]
    finally:
        # 收掉信箱：runId 是一次性的，留着只会长草
        with _lock:
            _slots.pop(run_id, None)


async def wait(run_id: str, timeout: float = DEFAULT_TIMEOUT) -> dict | None:
    """`wait_blocking` 的异步版（给将来异步的调用方）。

    超时返回 `None`（**不要**把 None 当成"跑成功了"）。
    """
    if not run_id:
        return None
    return await asyncio.to_thread(wait_blocking, run_id, float(timeout))


#: 一次运行最多留几张图（base64 会写进库里那条运行，三张足够说明问题）
MAX_IMAGES = 3
#: 单张 base64 的字符上限（约 1.5MB 的 PNG）—— 超了宁可丢掉，
#: 也不让一条运行把库撑爆
MAX_IMAGE_CHARS = 2_000_000


def _clean_images(raw) -> list[str]:  # noqa: ANN001
    """把上报的图收干净：限张数、限大小。

    注意这些 base64 **只往库里存**（零件上的 `run.images`），**不进模型上下文** ——
    喂给模型的那条工具结果只取 `text`（见 agent_loop 里那段拼接）。
    """
    out: list[str] = []
    for item in list(raw or []):
        if len(out) >= MAX_IMAGES:
            break  # 收够三张**有效的**就停（先滤后截：超大的那张不该占掉名额）
        text = str(item or "").strip()
        if not text or len(text) > MAX_IMAGE_CHARS:
            continue
        out.append(text)
    return out


def deliver(run_id: str, payload: dict) -> bool:
    """页面把输出报回来了。返回**是否有人正在等** `False` = 这一轮早结束了。"""
    if not run_id:
        return False
    body = dict(payload or {})
    body["images"] = _clean_images(body.get("images"))
    with _lock:
        slot = _slots.get(run_id)
        if slot is None:
            return False
        slot["payload"] = body
        slot["event"].set()
        return True


def pending() -> list[str]:
    """正在等输出的 runId（排障用）。"""
    with _lock:
        return [key for key, slot in _slots.items() if not slot["event"].is_set()]


__all__ = ["DEFAULT_TIMEOUT", "deliver", "pending", "wait", "wait_blocking"]
