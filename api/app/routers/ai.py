"""AI 批改的服务端代理 —— **每个用户用自己填的密钥**。

## 为什么仍然经过服务端，而不是浏览器直连供应商

纯粹是跨域：不少模型供应商的接口不允许浏览器直接调用（没有 CORS 头），
前端直连在浏览器里根本发不出去。所以服务端在这里只做「转发 + 带上调用者自己的
密钥」，不持有、也不提供任何共享密钥。

## 密钥从哪来

来自**调用者自己的** `user_settings.data.ai`（就是设置面板里那三个输入框）。
服务端只保留三项与个人凭据无关的策略：

* `ai_enabled`：实例级总开关，关掉后所有人都不能用（运维用）
* `ai_daily_quota`：**每人**每日调用上限，0 表示不限
* `ai_timeout_ms`：超时上限，用户不能把自己的超时设得比它更长

这样做的好处：谁的额度谁负责，不存在「别人注册个账号就把站长的密钥刷光」
这件事，站长也不必替别人保管计费凭据。

## 代价（要说清楚）

密钥会存在服务端数据库里（按用户隔离），因此**数据库泄露会连带泄露用户的密钥**。
这是「跨设备免重填」换来的：用户在手机上填一次，笔记本打开就能用。
如果哪天觉得这个交换不划算，退路是让浏览器只把密钥留在本地、每次请求随头上送
（服务端不落库），代价是每台设备都要重填一次。
"""

from __future__ import annotations

import time

import httpx
from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from ..config import get_settings
from ..deps import AuthenticatedWriter, CurrentUser, DbSession
from ..models import AiUsage, UserSettings

router = APIRouter(prefix="/api/ai", tags=["ai"])

# 用户没在设置里填接口地址时的默认值（OpenAI 兼容）
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_TIMEOUT_MS = 60000
MIN_TIMEOUT_MS = 5000


def _resolve_config(db: DbSession, user_id) -> dict:  # noqa: ANN001
    """取出调用者自己的 AI 配置。

    缺失或未填密钥时抛 503 并给出**可操作的**提示 —— 前端会把它原样显示给用户，
    所以文案要说清「去哪儿填」，而不是「未配置」这种没有下一步的话。
    """
    settings = get_settings()
    if not settings.ai_enabled:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "本站已关闭 AI 批改功能。")

    row = db.get(UserSettings, user_id)
    conf = ((row.data if row else {}) or {}).get("ai") or {}

    if not conf.get("enabled"):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "尚未启用 AI 批改：打开「设置 → AI 批改」并填入你自己的 API 密钥。",
        )

    api_key = str(conf.get("apiKey") or "").strip()
    base_url = str(conf.get("baseUrl") or "").strip() or DEFAULT_BASE_URL
    model = str(conf.get("model") or "").strip() or DEFAULT_MODEL

    if not api_key:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "还没有填写 API 密钥：打开「设置 → AI 批改」，填入你自己的密钥即可。",
        )

    timeout_ms = int(conf.get("timeoutMs") or DEFAULT_TIMEOUT_MS)
    timeout_ms = max(MIN_TIMEOUT_MS, min(timeout_ms, settings.ai_timeout_ms))

    return {"apiKey": api_key, "baseUrl": base_url, "model": model, "timeoutMs": timeout_ms}


def _endpoint(base_url: str) -> str:
    return base_url.rstrip("/") + "/chat/completions"


def _today_usage(db: DbSession, user_id) -> int:  # noqa: ANN001
    from datetime import date as date_type

    return (
        db.scalar(
            select(AiUsage.calls).where(
                AiUsage.user_id == user_id, AiUsage.date == date_type.today()
            )
        )
        or 0
    )


@router.post("/grade")
def grade(payload: dict, user: AuthenticatedWriter, db: DbSession) -> dict:
    """用**调用者自己的密钥**转发一次 chat completions，返回供应商的原始响应体。

    返回体刻意不做二次包装：前端 `ai.js` 的解析逻辑因此完全不用改。
    """
    settings = get_settings()
    conf = _resolve_config(db, user.id)

    # 每人每日上限，0 表示不限。它约束的是「同一个人刷太多」，
    # 而不是「替别人兜额度」—— 后者在自带密钥的模式下不存在。
    if settings.ai_daily_quota:
        used = _today_usage(db, user.id)
        if used >= settings.ai_daily_quota:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                f"今日 AI 调用已达上限（{settings.ai_daily_quota} 次），明天再来或改用「自己判断」。",
            )

    body = dict(payload or {})
    # 客户端传来的凭据一律忽略：用调用者设置里那份，服务端不提供共享密钥兜底
    for key in ("api_key", "apiKey", "base_url", "baseUrl", "authorization"):
        body.pop(key, None)
    body["model"] = conf["model"]
    body.setdefault("stream", False)

    url = _endpoint(conf["baseUrl"])
    started = time.perf_counter()

    try:
        with httpx.Client(timeout=conf["timeoutMs"] / 1000) as client:
            response = client.post(
                url,
                json=body,
                headers={
                    "Authorization": f"Bearer {conf['apiKey']}",
                    "Content-Type": "application/json",
                },
            )
    except httpx.TimeoutException:
        _record(db, user, ok=False, latency_ms=_ms(started), detail="超时")
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, "AI 服务响应超时") from None
    except httpx.HTTPError as exc:
        _record(db, user, ok=False, latency_ms=_ms(started), detail=str(exc)[:200])
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"无法连接你填写的接口：{exc}") from None

    latency_ms = _ms(started)

    if response.status_code >= 400:
        detail = response.text[:300]
        _record(db, user, ok=False, latency_ms=latency_ms, detail=detail)
        # 状态码原样透传：前端已经按 401/404/429 区分提示，
        # 用户看到「你的密钥被拒」比看到「服务端错误」有用得多
        raise HTTPException(
            response.status_code,
            f"接口返回 {response.status_code}：{detail}",
        )

    data = response.json()
    usage = data.get("usage") or {}
    _record(
        db,
        user,
        ok=True,
        latency_ms=latency_ms,
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
    )
    return data


@router.get("/ping")
def ping(user: CurrentUser, db: DbSession) -> dict:
    """连通性测试：用调用者自己的配置发一条极短的请求。

    返回字段与前端 `ai.ping()` 一致（ok / latencyMs / model / sample / message /
    url），设置面板因此不需要第二套展示逻辑。
    """
    settings = get_settings()
    try:
        conf = _resolve_config(db, user.id)
    except HTTPException as exc:
        return {
            "ok": False,
            "latencyMs": 0,
            "message": exc.detail,
            "url": "",
            "perUser": True,
        }

    started = time.perf_counter()
    try:
        with httpx.Client(timeout=min(conf["timeoutMs"], settings.ai_timeout_ms) / 1000) as client:
            response = client.post(
                _endpoint(conf["baseUrl"]),
                json={
                    "model": conf["model"],
                    "messages": [{"role": "user", "content": "只回复两个字：可用"}],
                    "temperature": 0,
                    "max_tokens": 16,
                    "stream": False,
                },
                headers={
                    "Authorization": f"Bearer {conf['apiKey']}",
                    "Content-Type": "application/json",
                },
            )
    except httpx.HTTPError as exc:
        return {
            "ok": False,
            "latencyMs": _ms(started),
            "message": str(exc)[:200],
            "url": conf["baseUrl"],
            "perUser": True,
        }

    latency_ms = _ms(started)
    if response.status_code >= 400:
        return {
            "ok": False,
            "latencyMs": latency_ms,
            "message": f"HTTP {response.status_code}：{response.text[:200]}",
            "url": conf["baseUrl"],
            "perUser": True,
        }

    data = response.json()
    choices = data.get("choices") or [{}]
    content = ((choices[0].get("message") or {}).get("content") or "").strip()
    return {
        "ok": True,
        "latencyMs": latency_ms,
        "model": data.get("model") or conf["model"],
        "sample": content,
        "url": conf["baseUrl"],
        "perUser": True,
    }


@router.get("/usage")
def usage(user: CurrentUser, db: DbSession) -> dict:
    """当前用户今日的 AI 用量。

    设置面板据此显示「今天已经用了多少次」，也顺带告诉用户
    「现在的密钥是不是你自己填的」。
    """
    settings = get_settings()
    row = db.get(UserSettings, user.id)
    conf = ((row.data if row else {}) or {}).get("ai") or {}
    return {
        "today": _today_usage(db, user.id),
        "quota": settings.ai_daily_quota,
        "enabled": settings.ai_enabled and bool(conf.get("enabled")),
        "model": str(conf.get("model") or "") or DEFAULT_MODEL,
        "hasKey": bool(str(conf.get("apiKey") or "").strip()),
    }


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _record(  # noqa: ANN001
    db: DbSession,
    user,  # noqa: ANN001
    *,
    ok: bool,
    latency_ms: int,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    detail: str = "",
) -> None:
    """把这次调用累加进当天的用量行。

    失败也要计数 —— 「为什么额度突然用完了」这个问题，答案往往是一串失败重试。
    用数据库侧的自增（`calls = calls + 1`）而不是「读出来加一再写回」：
    同一用户并发发请求时，后者会丢计数。
    """
    from datetime import date as date_type

    from sqlalchemy import func
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    today = date_type.today()
    db.execute(
        pg_insert(AiUsage)
        .values(
            user_id=user.id,
            date=today,
            calls=1,
            failures=0 if ok else 1,
            prompt_tokens=max(prompt_tokens, 0),
            completion_tokens=max(completion_tokens, 0),
            last_latency_ms=latency_ms,
            last_error="" if ok else detail[:300],
        )
        .on_conflict_do_update(
            index_elements=[AiUsage.user_id, AiUsage.date],
            set_={
                "calls": AiUsage.calls + 1,
                "failures": AiUsage.failures + (0 if ok else 1),
                "prompt_tokens": AiUsage.prompt_tokens + max(prompt_tokens, 0),
                "completion_tokens": AiUsage.completion_tokens + max(completion_tokens, 0),
                "last_latency_ms": latency_ms,
                "last_error": "" if ok else detail[:300],
                "updated_at": func.now(),
            },
        )
    )
    db.commit()
