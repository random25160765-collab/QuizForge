"""LLM 客户端：OpenAI 兼容协议、httpx 直连、不引各家 SDK（pipeline.md §7）。

三个能力走同一个客户端：
  * chat()   文本理解 / 出题 / 校验
  * vision() 读图（图片以 data URL 内联）
  * embed()  向量（调用方负责缓存，见 store 的 embeddings 表）

密钥只在 Authorization 头里出现，任何日志、异常信息都不打印它。
"""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import random
import re
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .config import LLMConfig

RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}

FENCE_RE = re.compile(r"^\s*```[A-Za-z0-9_+-]*\s*\n(.*?)\n\s*```\s*$", re.S)


class LLMError(RuntimeError):
    pass


@dataclass
class Reply:
    text: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    finish_reason: str = ""
    raw_usage: dict = field(default_factory=dict)


def strip_fence(text: str) -> str:
    """模型很爱把 JSON/YAML 包在 ``` 里，剥掉。"""
    match = FENCE_RE.match(text.strip())
    return match.group(1).strip() if match else text.strip()


def parse_json(text: str):
    """解析模型返回的 JSON。

    模型常见两种噪声：包在 ``` 里、或者在 JSON 后面又补一段解释。
    所以先剥围栏，再退回 raw_decode —— 取第一个完整的 JSON 值，忽略尾随内容。
    """
    cleaned = strip_fence(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        try:
            value, _end = json.JSONDecoder().raw_decode(cleaned)
            return value
        except json.JSONDecodeError:
            raise LLMError(f"返回不是合法 JSON（{exc}）：{cleaned[:300]}") from exc


class AdaptiveLimiter:
    """自适应并发闸门（AIMD）：把上游的并发吃满，又不在被限流时空转。

    为什么不能用固定并发数：定低了浪费带宽（一次出题 20 秒，几十个包要串着跑完），
    定高了持续 429、退避越滚越大 —— 而**上游的限流是动态的**，所以闸门也必须动态：
    成功就 +1（additive increase），被限流(429)或超时就砍半（multiplicative decrease）。
    稳态会贴着上游的上限来回小幅摆动，那正是"吃满带宽"的样子。

    `peak` 与 `throttled` 是事后判断的依据：峰值明显低于 max 且没被限流过，
    说明瓶颈在我们自己这边（不是上游）；throttled 很大则说明起始值定高了。
    """

    def __init__(self, start: int, minimum: int = 2, maximum: int = 64):
        self.limit = max(minimum, min(start, maximum))
        self.minimum = minimum
        self.maximum = maximum
        self.peak = self.limit
        self.throttled = 0
        self._active = 0
        self._cond = asyncio.Condition()

    async def __aenter__(self) -> "AdaptiveLimiter":
        async with self._cond:
            await self._cond.wait_for(lambda: self._active < self.limit)
            self._active += 1
        return self

    async def __aexit__(self, *exc) -> None:
        async with self._cond:
            self._active -= 1
            self._cond.notify_all()

    async def grow(self) -> None:
        async with self._cond:
            if self.limit < self.maximum:
                self.limit += 1
                self.peak = max(self.peak, self.limit)
            self._cond.notify_all()

    async def shrink(self) -> None:
        async with self._cond:
            self.throttled += 1
            self.limit = max(self.minimum, self.limit // 2)
            self._cond.notify_all()

    def describe(self) -> str:
        return f"并发闸门 峰值 {self.peak} / 上限 {self.maximum} · 被限流 {self.throttled} 次"


class LLM:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        # 连接池必须比闸门上限宽：否则闸门开到 32、HTTP 层只放 16 条连接，
        # 多出来的请求会静默排队 —— 看起来并发拉满了，其实卡在池子上。
        self.limit = AdaptiveLimiter(cfg.concurrency, maximum=cfg.max_concurrency)
        self._client = httpx.AsyncClient(
            base_url=cfg.base_url,
            timeout=httpx.Timeout(cfg.timeout_s, connect=20.0),
            headers={
                "Authorization": f"Bearer {cfg.api_key}",
                "Content-Type": "application/json",
            },
            limits=httpx.Limits(max_connections=cfg.max_concurrency + 8),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "LLM":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    # ------------------------------------------------------------- 底层

    async def _post(self, path: str, payload: dict) -> Reply:
        attempt = 0
        last_error = ""
        while attempt < 6:
            attempt += 1
            try:
                # 所有请求过**同一道**闸门：并发是全局的，不是"每个 worker 各自 4 路"。
                async with self.limit:
                    resp = await self._client.post(path, json=payload)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                await self.limit.shrink()  # 超时同样是压力信号
                await asyncio.sleep(min(30.0, 2 ** attempt) + random.random())
                continue

            if resp.status_code in RETRY_STATUS:
                retry_after = resp.headers.get("retry-after", "")
                try:
                    delay = float(retry_after)
                except ValueError:
                    delay = min(30.0, 2 ** attempt) + random.random()
                last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                if resp.status_code in (429, 503):
                    await self.limit.shrink()  # 真被限流：砍半再退避
                await asyncio.sleep(max(0.5, delay))
                continue

            if resp.status_code >= 400:
                # 400 且提到 response_format：换个不带 json 模式的请求再试一次
                body = resp.text[:400]
                if resp.status_code == 400 and "response_format" in body and "response_format" in payload:
                    payload = {k: v for k, v in payload.items() if k != "response_format"}
                    attempt -= 1
                    if attempt < 0:
                        attempt = 0
                    continue
                raise LLMError(f"HTTP {resp.status_code}: {body}")

            # 成功就加一点并发（AIMD 的 additive increase）—— 少了这一句，闸门只会缩不会涨，
            # 于是永远停在起始值（实测峰值卡在 16，64 个 worker 里有 48 个在干等）。
            await self.limit.grow()
            data = resp.json()
            choices = data.get("choices") or []
            if not choices:
                raise LLMError(f"返回里没有 choices：{str(data)[:300]}")
            usage = data.get("usage") or {}
            return Reply(
                text=(choices[0].get("message") or {}).get("content") or "",
                model=data.get("model", payload.get("model", "")),
                tokens_in=int(usage.get("prompt_tokens") or 0),
                tokens_out=int(usage.get("completion_tokens") or 0),
                finish_reason=choices[0].get("finish_reason") or "",
                raw_usage=usage,
            )
        raise LLMError(f"重试 6 次仍失败：{last_error}")

    # ------------------------------------------------------------- 能力

    async def chat(
        self,
        messages: list[dict],
        *,
        model: str | None = None,
        json_mode: bool = True,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> Reply:
        payload: dict = {
            "model": model or self.cfg.model,
            "messages": messages,
            "temperature": temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if max_tokens:
            payload["max_tokens"] = max_tokens
        return await self._post("/chat/completions", payload)

    async def vision(
        self,
        image_path: str | Path,
        prompt: str,
        *,
        model: str | None = None,
        json_mode: bool = True,
    ) -> Reply:
        path = Path(image_path)
        # **MIME 按魔数认，不只看扩展名**：浏览器保存网页时 `*_files/` 里的图常常
        # 没有扩展名（`rcdszc74add_qnwhvt8…` 这种），`guess_type` 猜不出来，
        # 而给它一个不符的 MIME 上游可能直接拒。
        data = path.read_bytes()
        head = data[:16]
        sniffed = (
            "image/png" if head.startswith(b"\x89PNG")
            else "image/jpeg" if head.startswith(b"\xff\xd8\xff")
            else "image/gif" if head.startswith(b"GIF8")
            else "image/webp" if head.startswith(b"RIFF") and head[8:12] == b"WEBP"
            else ""
        )
        mime = sniffed or mimetypes.guess_type(path.name)[0] or "image/webp"
        data_url = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ]
        return await self._post(
            "/chat/completions",
            {
                "model": model or self.cfg.vision_model,
                "messages": messages,
                "temperature": 0.1,
                **({"response_format": {"type": "json_object"}} if json_mode else {}),
            },
        )

    async def embed(self, texts: list[str], *, model: str | None = None) -> list[list[float]]:
        """向量化。/embeddings 的返回体没有 choices，所以这里自己走一遍重试。"""
        if not texts:
            return []
        payload = {"model": model or self.cfg.embed_model, "input": texts}
        last_error = ""
        for attempt in range(1, 7):
            try:
                resp = await self._client.post("/embeddings", json=payload)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(min(30.0, 2 ** attempt) + random.random())
                continue
            if resp.status_code in RETRY_STATUS:
                last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                await asyncio.sleep(min(30.0, 2 ** attempt) + random.random())
                continue
            if resp.status_code >= 400:
                raise LLMError(f"HTTP {resp.status_code}: {resp.text[:400]}")
            data = resp.json()
            items = data.get("data") or []
            if not items:
                raise LLMError(f"embedding 返回为空：{str(data)[:200]}")
            return [list(item.get("embedding") or []) for item in items]
        raise LLMError(f"embedding 重试 6 次仍失败：{last_error}")
