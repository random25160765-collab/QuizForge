"""AI 批改的服务端代理 —— **每个用户用自己填的密钥**。

## 为什么仍然经过服务端，而不是浏览器直连供应商

纯粹是跨域：不少模型供应商的接口不允许浏览器直接调用（没有 CORS 头），
前端直连在浏览器里根本发不出去。所以服务端在这里只做「转发 + 带上调用者自己的
密钥」，不持有、也不提供任何共享密钥。

## 密钥从哪来

来自**调用者自己的** `app_settings.data.ai`（就是设置面板里那三个输入框）。
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

## 与对话的关系

「取配置 / 查配额 / 记账」这三件事的实现在 `app/ai_gateway.py` —— 对话
（`routers/chat.py`）走的是**同一份**，免得两个入口的配额口径不一致。
本文件只剩两件与批改有关的事：非流式的透传、以及连通性探测。
"""

from __future__ import annotations

import time

import httpx
from fastapi import APIRouter, HTTPException, status

from .. import ai_gateway as gateway
from ..config import get_settings
from ..deps import DbSession
from ..settings_store import row as settings_row

router = APIRouter(prefix="/api/ai", tags=["ai"])


@router.post("/grade")
def grade(payload: dict, db: DbSession) -> dict:
    """用**本机设置里那份密钥**转发一次 chat completions，返回供应商的原始响应体。

    返回体刻意不做二次包装：前端 `ai.js` 的解析逻辑因此完全不用改。
    """
    conf = gateway.resolve_config(db)
    gateway.enforce_quota(db)

    body = dict(payload or {})
    # 客户端传来的凭据一律忽略：用调用者设置里那份，服务端不提供共享密钥兜底
    for key in ("api_key", "apiKey", "base_url", "baseUrl", "authorization"):
        body.pop(key, None)
    body["model"] = conf["model"]
    body.setdefault("stream", False)

    url = gateway.completion_endpoint(conf["baseUrl"])
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
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        # **够不着上游**（DNS 失败 / 连接被拒 / 连接阶段就超时）—— 与"上游慢"
        # 是两件事，所以这条必须排在下面的 `TimeoutException` **之前**。
        #
        # `ConnectTimeout` 的父类是 `TimeoutException` 而**不是** `ConnectError`，
        # 所以要显式列两个；否则它被超时那条吞掉，用户只看到"响应超时"，
        # 而最该给的那半句指点（走内测通道的人去找站长、自带密钥的人查地址与网络）
        # 恰恰丢了 —— 2026-09-22 由 `test_beta_channel` 那几条测试发现。
        gateway.record_usage(
            db, ok=False, latency_ms=gateway.elapsed_ms(started), detail=f"连不上：{exc}"[:200]
        )
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            gateway.connection_hint(conf) + "（" + str(exc)[:120] + "）",
        ) from None
    except httpx.TimeoutException:
        # 连上了、但没按时回完 —— 这是"慢"，重试有意义（与上面那条不同）
        gateway.record_usage(
            db, ok=False, latency_ms=gateway.elapsed_ms(started), detail="超时"
        )
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, "AI 服务响应超时") from None
    except httpx.HTTPError as exc:
        gateway.record_usage(
            db, ok=False, latency_ms=gateway.elapsed_ms(started), detail=str(exc)[:200]
        )
        # 其它传输层错误（协议错、连接被中途掐断…）：同样属于"够不着"，
        # 该说的话与上面第一条一样，取决于用的是自己的密钥还是内测通道
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            gateway.connection_hint(conf) + "（" + str(exc)[:120] + "）",
        ) from None

    latency_ms = gateway.elapsed_ms(started)

    if response.status_code >= 400:
        detail = response.text[:300]
        gateway.record_usage(db, ok=False, latency_ms=latency_ms, detail=detail)
        # 状态码原样透传：前端已经按 401/404/429 区分提示，
        # 用户看到「你的密钥被拒」比看到「服务端错误」有用得多。
        #
        # 一个例外：**5xx 且 body 是空的** —— 那通常不是模型供应商在说话，
        # 而是路上的代理/网关替它回的（实测：本机代理会把一个根本连不上的地址
        # 变成一个空体 502）。这时只说"接口返回 502："等于什么都没说，
        # 得把 connection_hint 那半句补上，否则用户不知道该找谁。
        message = f"接口返回 {response.status_code}：{detail}"
        if response.status_code >= 500 and not detail.strip():
            message = (
                gateway.connection_hint(conf)
                + f"（网关回了 {response.status_code}，没有内容）"
            )
        raise HTTPException(response.status_code, message)

    data = response.json()
    usage = data.get("usage") or {}
    gateway.record_usage(
        db,
        ok=True,
        latency_ms=latency_ms,
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
    )
    return data


@router.get("/ping")
def ping(db: DbSession) -> dict:
    """连通性测试：用本机那份配置发一条极短的请求。

    返回字段与前端 `ai.ping()` 一致（ok / latencyMs / model / sample / message /
    url），设置面板因此不需要第二套展示逻辑。
    """
    settings = get_settings()
    try:
        conf = gateway.resolve_config(db)
    except HTTPException as exc:
        return {
            "ok": False,
            "latencyMs": 0,
            "message": exc.detail,
            "url": "",
            "perUser": True,
            "source": "none",
            "label": "",
        }

    started = time.perf_counter()
    try:
        with httpx.Client(timeout=min(conf["timeoutMs"], settings.ai_timeout_ms) / 1000) as client:
            response = client.post(
                gateway.completion_endpoint(conf["baseUrl"]),
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
            "latencyMs": gateway.elapsed_ms(started),
            "message": gateway.connection_hint(conf) + "（" + str(exc)[:120] + "）",
            "url": conf["baseUrl"],
            "perUser": True,
            "source": conf["source"],
            "label": conf["label"],
        }

    latency_ms = gateway.elapsed_ms(started)
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
        "source": conf["source"],
        "label": conf["label"],
    }


@router.get("/usage")
def usage(db: DbSession) -> dict:
    """今天这台机器的 AI 用量。

    设置面板据此显示「今天已经用了多少次」，也顺带告诉用户
    「现在的密钥是不是你自己填的」。
    """
    settings = get_settings()
    row = settings_row(db)
    conf = ((row.data if row else {}) or {}).get("ai") or {}

    has_key = bool(str(conf.get("apiKey") or "").strip())
    uses_own = bool(conf.get("enabled")) and has_key
    beta = gateway.beta_config() or {}
    beta_ready = bool(str(beta.get("apiKey") or "").strip()) and beta.get("enabled", True) is not False

    if not settings.ai_enabled:
        mode = "off"
    elif uses_own:
        mode = "user"
    elif settings.ai_beta_enabled and beta_ready:
        mode = "beta"
    else:
        mode = "none"

    beta_model = str(beta.get("model") or "").strip()
    return {
        "today": gateway.today_usage(db),
        "quota": settings.ai_daily_quota,
        # enabled 现在的意思是「现在能不能用」，而不是「你自己那个开关开没开」——
        # 内测通道让没填密钥的人也能用，面板与对话页要能说清是哪种情况
        "enabled": mode in ("user", "beta"),
        "mode": mode,
        "label": "你自己的密钥"
        if uses_own
        else (str(beta.get("label") or "").strip() or ("内测通道 · " + beta_model) if mode == "beta" else ""),
        "betaModel": beta_model if mode == "beta" else "",
        "model": str(conf.get("model") or "") or gateway.DEFAULT_MODEL,
        "hasKey": has_key,
    }
