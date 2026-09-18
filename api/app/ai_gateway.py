"""AI 网关 —— 与模型供应商打交道时**共用的那一半**。

批改（`routers/ai.py`）与对话（`routers/chat.py`）都从这里取配置、查配额、记账。
放在一处，是为了让两个入口不可能出现「一个算了配额、另一个没算」这种偏差 ——
这类偏差在自用阶段没人会发现，等到多租户时就是白送的账。

## 谁付钱

每个用户用**自己**的密钥（`user_settings.data.ai`）。服务端只保留三项与个人凭据
无关的策略：`ai_enabled`（实例总开关）/ `ai_daily_quota`（每人每日上限，0 不限）/
`ai_timeout_ms`（超时上限）。谁的额度谁负责，站长不必替别人保管计费凭据。

代价也写在这里：密钥会按用户隔离地存在库里，**数据库泄露会连带泄露用户的密钥**。
这是「跨设备免重填」换来的；退路是浏览器只把密钥留在本地、每次随头上送。

## 非流式的调用为什么不在这里

`routers/ai.py` 的批改是一次性透传（发出去、拿回整个 JSON、原样交给前端解析），
与流式的控制流确实不同。为了共享而给同一个函数加一个 `stream` 开关，
只会让两边都多出一层 `if`。所以这里只管**策略**与**流式**：

    resolve_config / enforce_quota / record_usage / today_usage / completion_endpoint / elapsed_ms
    stream_completion                     ← 流式（对话用）
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx

from .config import get_settings
from .models import AiUsage, UserSettings

# 内测通道那份配置的缓存：{path, mtime, data}
# 按 mtime 判断，所以改完文件立刻生效，不用重启服务
_BETA_CACHE: dict = {"path": "", "mtime": 0.0, "data": {}}

# 用户没在设置里填接口地址时的默认值（OpenAI 兼容）
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"

# 能读图的模型（按名字判断）。认不出就当读不了 —— 往一个不支持视觉的接口塞
# image_url，上游会直接 400，那比"读不到图"更糟。
# 用户也可以在自己的设置里显式写 `"vision": true/false` 覆盖这个判断。
#
# `deepseek` 在表里是**实测加上的**：名字里带 "chat" 让人以为它是纯文本，
# 但它已经支持图像输入（发一张成绩表过去，它把课程名与学分列都读出来了）。
# 这条教训写在这儿：**模型名推不出能力**，所以既有保守的默认，也有显式开关。
_VISION_HINTS = (
    "deepseek",
    "gpt-4o",
    "gpt-4.1",
    "gpt-5",
    "o3",
    "o4",
    "claude",
    "gemini",
    "glm-4v",
    "vision",
    "internvl",
    "llava",
    "pixtral",
    "-vl",
    "vl-",
)

# 上游真正接受的图像格式（实测：不在其中的会回 400 "unsupported image"）。
# `attachments.IMAGE_SUFFIXES` 比这个宽（含 bmp/svg）——那些存得下、给不了模型。
VISION_MIMES = ("image/png", "image/jpeg", "image/webp", "image/gif")


def model_reads_images(model: str) -> bool:
    """这个模型能不能读图。"""
    name = (model or "").lower()
    return any(hint in name for hint in _VISION_HINTS)


def _vision_of(conf: dict, model: str) -> bool:
    """能不能读图：配置里显式写了就听配置，否则按模型名判断。

    留这个显式开关是因为自动判断必然有漏（自建网关上的模型名千奇百怪），
    而猜错的代价是双向的：猜"能读"会给上游塞 image_url（400），
    猜"不能读"则白白浪费一个本来能看图的模型。
    """
    flag = conf.get("vision")
    if isinstance(flag, bool):
        return flag
    return model_reads_images(model)
DEFAULT_TIMEOUT_MS = 60000
MIN_TIMEOUT_MS = 5000
# 低于这个预算就装不下"系统提示 + 一轮问答"，设更小没有意义
MIN_CONTEXT_TOKENS = 1000

# 流式的读超时（秒）。它是**两次读之间**的上限，不是总时长 —— 一直吐就一直等。
# 于是一个值同时管两件事：等首字节要等多久才判「通道没响应」，以及中途多久没吐字算断流。
# 不设 1 秒：首 token 之前模型在推理、工具分片之间也有空隙（实测能到十几秒），
# 误杀比漏杀难查得多。也不能拿整份预算当读超时 —— 那等于让用户干等满 90 秒。
STREAM_READ_TIMEOUT_S = 20.0
STREAM_CONNECT_TIMEOUT_S = 10.0
STREAM_WRITE_TIMEOUT_S = 30.0
STREAM_POOL_TIMEOUT_S = 10.0


class UpstreamError(Exception):
    """上游（模型供应商）出错。

    刻意带三个字段而不是只带一句话：调用方要据此决定**回什么**给用户 ——
    超时该说"稍后重试"，密钥被拒该把上游的原文带出来（用户看到「你的密钥被拒」
    比看到「服务端错误」有用得多）。

    * ``kind``：``timeout`` / ``connect`` / ``http``
    * ``status``：上游 HTTP 状态码（不是 http 类错误时为 None）
    * ``detail``：上游响应体截断后的原文，用于展示与排查
    """

    def __init__(self, message: str, *, kind: str = "http", status: int | None = None, detail: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.detail = detail or message


def beta_config() -> dict | None:
    """读内测通道那份实例级配置（与用户设置里的 `ai` 同形）。

    * 关掉通道（`QF_AI_BETA_ENABLED=false`）或文件不在 → None
    * 文件按 **mtime** 缓存：改完立刻生效，不必重启
    * 坏文件当成"没有"而不是抛异常 —— 一个手写的 JSON 不该让整站 AI 全挂

    密钥只在这里落地，**不进 Settings**：那个对象会被 repr、被日志打印。
    """
    settings = get_settings()
    if not settings.ai_beta_enabled:
        return None

    path = Path(settings.ai_beta_config_file)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None

    if _BETA_CACHE["path"] == str(path) and _BETA_CACHE["mtime"] == mtime and _BETA_CACHE["data"]:
        return _BETA_CACHE["data"]

    try:
        data = json.loads(path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    _BETA_CACHE.update({"path": str(path), "mtime": mtime, "data": data})
    return data


def resolve_config(db, user_id) -> dict:  # noqa: ANN001
    """取出这次调用该用哪套配置：**用户自己的密钥 > 内测通道 > 明确报错**。

    返回值多一个 `source`（`user` / `local`）与 `label`：调用方要靠它决定
    "连不上时该叫人去启动本地模型，还是去检查自己的密钥" —— 这两种失败的
    下一步动作完全不同，提示语不能混。

    缺失且没有内测通道时抛 503，文案必须**可操作**：前端会把它原样显示给用户，
    所以要说清「去哪儿填」，而不是「未配置」这种没有下一步的话。
    """
    from fastapi import HTTPException, status as http_status

    settings = get_settings()
    if not settings.ai_enabled:
        raise HTTPException(http_status.HTTP_503_SERVICE_UNAVAILABLE, "本站已关闭 AI 功能。")

    row = db.get(UserSettings, user_id)
    conf = ((row.data if row else {}) or {}).get("ai") or {}

    api_key = str(conf.get("apiKey") or "").strip()
    timeout_ms = int(conf.get("timeoutMs") or DEFAULT_TIMEOUT_MS)
    timeout_ms = max(MIN_TIMEOUT_MS, min(timeout_ms, settings.ai_timeout_ms))
    # 上下文预算：用户给多少都行（小窗口模型要它小），但不超过实例上限
    context_tokens = int(conf.get("maxContextTokens") or 0) or settings.ai_max_context_tokens
    context_tokens = max(MIN_CONTEXT_TOKENS, min(context_tokens, settings.ai_max_context_tokens))

    if conf.get("enabled") and api_key:
        model = str(conf.get("model") or "").strip() or DEFAULT_MODEL
        return {
            "apiKey": api_key,
            "baseUrl": str(conf.get("baseUrl") or "").strip() or DEFAULT_BASE_URL,
            "model": model,
            "vision": _vision_of(conf, model),
            "timeoutMs": timeout_ms,
            "maxContextTokens": context_tokens,
            "source": "user",
            "label": "你自己的密钥",
        }

    beta = beta_config()
    beta_key = str((beta or {}).get("apiKey") or "").strip()
    if beta and beta_key and beta.get("enabled", True) is not False:
        # 内测通道：用站长那份密钥。超时也用实例的 —— 用户自己那份没生效，
        # 拿他的超时来约束这条通道没有道理
        beta_timeout = int(beta.get("timeoutMs") or settings.ai_timeout_ms)
        model = str(beta.get("model") or "").strip() or DEFAULT_MODEL
        return {
            "apiKey": beta_key,
            "baseUrl": str(beta.get("baseUrl") or "").strip() or DEFAULT_BASE_URL,
            "model": model,
            "vision": _vision_of(beta, model),
            "timeoutMs": max(MIN_TIMEOUT_MS, min(beta_timeout, settings.ai_timeout_ms)),
            "maxContextTokens": context_tokens,
            "source": "beta",
            "label": str(beta.get("label") or "").strip() or ("内测通道 · " + model),
        }

    if beta is not None and not beta_key:
        # 文件在、但没密钥：这是配置写错了，得说清是哪儿，而不是让人去设置里翻
        raise HTTPException(
            http_status.HTTP_503_SERVICE_UNAVAILABLE,
            "内测通道的配置里没有 apiKey：检查 config/ai.local.json，"
            "或在「设置 → AI」填入你自己的密钥。",
        )

    if not conf.get("enabled"):
        raise HTTPException(
            http_status.HTTP_503_SERVICE_UNAVAILABLE,
            "尚未启用 AI：打开「设置 → AI」，填入你自己的 API 密钥。"
            "（本站的内测通道没有开，所以没有免密钥的用法。）",
        )

    raise HTTPException(
        http_status.HTTP_503_SERVICE_UNAVAILABLE,
        "还没有填写 API 密钥：打开「设置 → AI」，填入你自己的密钥即可。",
    )


def connection_hint(conf: dict) -> str:
    """连不上上游时该补的那半句话。

    「连不上」这件事，两种来源的下一步动作完全不同：用自己密钥的人该去查
    密钥与网络；走内测通道的人该去找站长（那是站长的密钥与额度）。
    写成同一句，两种人都会白折腾一遍。
    """
    if conf.get("source") == "beta":
        return (
            "内测通道连不上（"
            + str(conf.get("baseUrl") or "")
            + "）：把这个问题告诉站长（他那边检查 config/ai.local.json 与网络），"
            "或者在「设置 → AI」里填自己的密钥照样能用。"
        )
    return "无法连接你填写的接口：" + str(conf.get("baseUrl") or "")


def completion_endpoint(base_url: str) -> str:
    return base_url.rstrip("/") + "/chat/completions"


def today_usage(db, user_id) -> int:  # noqa: ANN001
    from datetime import date as date_type

    from sqlalchemy import select

    return (
        db.scalar(
            select(AiUsage.calls).where(
                AiUsage.user_id == user_id, AiUsage.date == date_type.today()
            )
        )
        or 0
    )


def enforce_quota(db, user_id) -> None:  # noqa: ANN001
    """每人每日上限，0 表示不限。

    它约束的是「同一个人刷太多」，而不是「替别人兜额度」——
    后者在自带密钥的模式下不存在。
    """
    from fastapi import HTTPException, status as http_status

    quota = get_settings().ai_daily_quota
    if quota and today_usage(db, user_id) >= quota:
        raise HTTPException(
            http_status.HTTP_429_TOO_MANY_REQUESTS,
            f"今日 AI 调用已达上限（{quota} 次），明天再来或改用「自己判断」。",
        )


def record_usage(  # noqa: ANN001
    db,
    user,
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
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    db.execute(
        sqlite_insert(AiUsage)
        .values(
            user_id=user.id,
            date=date_type.today(),
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
                "updated_at": func.current_timestamp(),
            },
        )
    )
    db.commit()


def elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def stream_completion(
    conf: dict,
    messages: list[dict],
    *,
    tools: list[dict] | None = None,
    params: dict | None = None,
):
    """向上游要一次**流式**回复，逐块产出 ``(kind, payload)``：

        ("delta", 增量文本)
        ("think", 增量推理)          # 有的模型给 reasoning_content
        ("tool_calls", [{id,name,arguments}])   # 一轮结束时给一次（若它要了工具）
        ("usage", {"promptTokens": int, "completionTokens": int})
        ("finish", 结束原因)        # stop / length / tool_calls …

    解析的是 OpenAI 兼容的 SSE（``data: {...}`` 行 + ``data: [DONE]``）。
    认不出来的行直接跳过 —— 各家都会往里塞私有字段（心跳、id、event:），
    为它们报错只会让"换一个供应商"变成一件难事。``usage`` 也同理：
    多数供应商只在最后一个块里给，给了就记账，没给就按 0 计。

    工具调用在流里是**分片**来的（name 一块、arguments 几个字符一块），
    所以要按 `index` 攒起来，攒完再一次性交出去 —— 这是 OpenAI 兼容协议
    最容易写错的一处，测试里专门钉了一条。

    超时用**读超时**（`STREAM_READ_TIMEOUT_S`）：若干秒没吐字就断，但一直吐就一直等。
    读超时是「两次读之间」的上限而不是总时长，所以它天然就是首字节超时 ——
    2026-09-17 实测：把整份预算（90 秒）当读超时用，通道无响应时界面要转满一分半才报错。
    这才是"生成慢"与"服务挂了"的正确区分方式。

    上游的非 2xx 一律抛 `UpstreamError`（带响应体截断原文）。
    """
    body = dict(params or {})
    body["model"] = conf["model"]
    body["messages"] = messages
    body["stream"] = True
    if tools:
        body["tools"] = tools
        body.setdefault("tool_choice", "auto")

    headers = {
        "Authorization": f"Bearer {conf['apiKey']}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }

    budget = max(1.0, conf["timeoutMs"] / 1000)
    timeout = httpx.Timeout(
        connect=min(STREAM_CONNECT_TIMEOUT_S, budget),
        read=min(STREAM_READ_TIMEOUT_S, budget),
        write=min(STREAM_WRITE_TIMEOUT_S, budget),
        pool=min(STREAM_POOL_TIMEOUT_S, budget),
    )
    received = 0  # 已吐出的正文字符数：用来区分「从没响应」与「吐到一半断了」
    started = time.perf_counter()

    try:
        with httpx.Client(timeout=timeout) as client:
            with client.stream(
                "POST", completion_endpoint(conf["baseUrl"]), json=body, headers=headers
            ) as response:
                if response.status_code >= 400:
                    detail = response.read().decode("utf-8", "replace")[:300]
                    raise UpstreamError(
                        f"接口返回 {response.status_code}",
                        kind="http",
                        status=response.status_code,
                        detail=detail,
                    )

                calls: dict[int, dict] = {}  # index → {id, name, arguments}

                for line in response.iter_lines():
                    line = line.strip()
                    if not line or line.startswith(":"):
                        continue  # 空行（事件分隔）与心跳注释
                    if not line.startswith("data:"):
                        continue  # event: / id: / retry: 之类，暂时用不上
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break

                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue  # 供应商夹带的非 JSON 片段，不因此中断整条流

                    if isinstance(chunk, dict) and chunk.get("usage"):
                        usage = chunk["usage"] or {}
                        yield (
                            "usage",
                            {
                                "promptTokens": int(usage.get("prompt_tokens") or 0),
                                "completionTokens": int(usage.get("completion_tokens") or 0),
                            },
                        )

                    for choice in (chunk.get("choices") or []) if isinstance(chunk, dict) else []:
                        delta = choice.get("delta") or {}
                        text = delta.get("content")
                        if text:
                            received += len(text)
                            yield ("delta", text)

                        # 推理增量：DeepSeek 系给 reasoning_content，另一些给 reasoning
                        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                        if reasoning:
                            yield ("think", reasoning)

                        if isinstance(delta, dict):
                            for fragment in delta.get("tool_calls") or []:
                                self_index = int(fragment.get("index") or 0)
                                slot = calls.setdefault(
                                    self_index, {"id": "", "name": "", "arguments": ""}
                                )
                                if fragment.get("id"):
                                    slot["id"] = str(fragment["id"])
                                function = fragment.get("function") or {}
                                if function.get("name"):
                                    slot["name"] = str(function["name"])
                                if function.get("arguments"):
                                    slot["arguments"] += str(function["arguments"])

                        reason = choice.get("finish_reason")
                        if reason:
                            yield ("finish", str(reason))

                if calls:
                    yield ("tool_calls", [calls[index] for index in sorted(calls)])
    except httpx.TimeoutException as exc:
        # 两种超时的下一步动作不同：一个字都没收到 → 该去查通道/网络；
        # 吐到一半断了 → 直接重试即可。文案混在一起会让排查从「看一眼」变成「猜」。
        #
        # 异常类名留在**文案里**而不是 detail 里：detail 的约定是「上游说的话」，
        # 而超时这件事上游一个字都没说（把 httpx 的 ConnectTimeout 塞进 detail 会在界面上
        # 显示成英文原文 —— 2026-09-18 实测踩过）。类名仍要留着：ConnectTimeout 是连不上、
        # ReadTimeout 是连上了不回，两者的下一步动作不同。
        waited = time.perf_counter() - started
        why = exc.__class__.__name__
        if received:
            raise UpstreamError(
                f"上游在 {STREAM_READ_TIMEOUT_S:.0f} 秒内没有继续返回"
                f"（已收到 {received} 字，{why}）",
                kind="timeout",
            ) from None
        raise UpstreamError(
            f"上游 {waited:.0f} 秒内没有任何响应（{why}）",
            kind="timeout",
        ) from None
    except httpx.HTTPError as exc:
        raise UpstreamError(str(exc)[:200], kind="connect") from None
