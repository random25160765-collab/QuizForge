"""AI 网关 —— 与模型供应商打交道时**共用的那一半**。

批改（`routers/ai.py`）与对话（`routers/chat.py`）都从这里取配置、查配额、记账。
放在一处，是为了让两个入口不可能出现「一个算了配额、另一个没算」这种偏差 ——
这类偏差在自用阶段没人会发现，等到多租户时就是白送的账。

## 谁付钱

**这台机器的主人**用自己的密钥（`app_settings.data.ai`），或者走内测通道
（`config/ai.local.json`，站长那份）。服务端只保留三项与凭据无关的策略：
`ai_enabled`（总开关）/ `ai_daily_quota`（每日上限，0 不限）/ `ai_timeout_ms`（超时上限）。

代价也写在这里：密钥存在库里（`app_settings.data.ai.apiKey`），
**这份库被谁看到就等于密钥被谁看到** —— 所以 `make db-snapshot` 落盘前会把它抹空
（`tools/db_snapshot.py`），`make check` 里还有一道密钥扫描兜底（`tools/secret_scan.py`）。

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
from .models import AiUsage
from .settings_store import row as settings_row

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


#: **模型名 → 上下文窗口**（token）。
#:
#: 为什么是自己维护一张表，而不是问接口要：`GET /models` **不回窗口** ——
#: 2026-09 在 api.deepseek.com 上实测，每个条目只有 `id / object / owned_by`
#: 三个键。既然"元数据"这条路不通，就把数字抄在本地：官方 Models & Pricing 页
#: 给的 `deepseek-flash` 与 `deepseek-v4-pro` 都是 **1M**。
#:
#: 比对用**前缀**（`deepseek-flash-0710` 这种带日期的也认）。表里没有的模型
#: 回落到实例上限（`settings.ai_max_context_tokens`）—— 新模型上来时站长改那
#: 一个数就能兜住，不必等发版。
MODEL_CONTEXT: dict[str, int] = {
    "deepseek-flash": 1_000_000,  # DeepSeek-V4.1-Flash
    "deepseek-v4-flash": 1_000_000,  # 旧名：官方仍接受，路由到 V4.1-Flash
    "deepseek-v4-pro": 1_000_000,
    # 下面两个已不在 `/models` 的返回里，但请求仍能调通（实测）——
    # 官方文档把 V4 之前的名字并进了 Flash 一档，所以按 1M 给。
    "deepseek-chat": 1_000_000,
    "deepseek-reasoner": 1_000_000,
}


def model_context(model: str, fallback: int) -> int:
    """这个模型能吃多少 token；认不出来就给 `fallback`。"""
    name = (model or "").strip().lower()
    for key, tokens in MODEL_CONTEXT.items():
        if name == key or name.startswith(key + "-"):
            return tokens
    return fallback


#: 认得出支持 `thinking` 开关的模型（前缀比对，与 `MODEL_CONTEXT` 同一套写法）。
#:
#: 为什么要一张名单：`thinking` 是 DeepSeek 的方言，往别家（或自建网关）的请求里
#: 塞一个它不认识的字段，有的会直接 400 —— 名单外一个字节都不加，行为与从前一致。
#:
#: `deepseek-chat` 也在名单里：2026-09-20 实测它**能被这个开关打开思考**
#:（裸调 0 字思维链，加 `thinking: enabled` 后 120 字）—— 它不是"不能思考"，
#: 只是默认不思考；而 `deepseek-flash` 裸调就思考（官方：思考模式默认开、
#: 默认 effort=high）。
THINKING_MODELS: tuple[str, ...] = (
    "deepseek-flash",
    "deepseek-v4-flash",
    "deepseek-v4-pro",
    "deepseek-chat",
    "deepseek-reasoner",
)


#: 思考强度：关 / 省 / 常规 / 使劲 —— **人按的那一档**。
#:
#: 用户原话："llm 有的时候会 overthinking，一个简单的问题思考特别久。给它加一个思考强度，
#: 在命令里面控制。"
#:
#: 上游真正认的只有一件事：`thinking: enabled|disabled`（`deepseek-flash` 裸调就思考、
#: 官方默认 effort=high；`deepseek-chat` 裸调不思考）—— 这是 2026-09-20 实测的，
#: 两个方向都得显式写。至于中间几档，2026-09-25 拿最小请求逐个试过：`effort` /
#: `level` / `reasoning_effort` 三种字段上游**一律回 200**（它静默忽略不认识的字段），
#: 而在"1+1"这种题上思维链长度的差异（103/120/104/105/131 字）是噪声 ——
#: **断定不了哪个真被采纳**。
#:
#: 所以这里的分工是诚实的：**"关"是硬开关**（真管用：思维链 0 字、也更快更省）；
#: **中间几档靠两样** —— 一并带 `reasoning_effort`（它若认就是赚的；不认也只是被忽略，
#: 实测不会 400），以及把"这一轮该想多深"写进系统提示
#: （见 `routers/chat.py` 的 `thinking_line`）。不猜上游，也不假装设了档位它就一定照办。
THINK_LEVELS: tuple[str, ...] = ("off", "low", "normal", "high")


def thinking_level(effort: object) -> str:
    """把外面递进来的东西收敛成四档之一。

    `True/False` 是历史写法（那颗「深度思考」药丸），照样认：`False` = 关，
    `True` = 常规（= 从前的"开"，行为与改这版之前一致）。
    """
    if isinstance(effort, bool):
        return "normal" if effort else "off"
    raw = str(effort or "").strip().lower()
    if raw in ("", "on", "true", "yes", "enabled", "deep"):
        return "normal"
    return raw if raw in THINK_LEVELS else "normal"


def thinking_params(model: str, effort: object = True) -> dict:
    """这次请求的思考参数。`effort` 收等级字符串（`off`/`low`/`normal`/`high`）或布尔。

    * `off` → `thinking: {type: disabled}`（实测思维链 0 字：这才是治 overthinking 的那一手）；
    * 其余三档 → `thinking: {type: enabled}` ＋ 一个 `reasoning_effort`
      （best-effort，理由见 `THINK_LEVELS` 上面那段）。

    名单外返回空字典：那个字段对别家没有意义，塞过去只会招 400（见 `THINKING_MODELS`）。
    """
    name = (model or "").strip().lower()
    if not any(name == key or name.startswith(key + "-") for key in THINKING_MODELS):
        return {}
    level = thinking_level(effort)
    if level == "off":
        return {"thinking": {"type": "disabled"}}
    out: dict = {"thinking": {"type": "enabled"}}
    if level in ("low", "high"):
        out["reasoning_effort"] = level
    return out


def budget_tokens(model: str, conf: dict, cap: int) -> int:
    """这次调用真正用多少上下文预算 —— **按模型自动定**，三个数取最小：

      * **模型窗口**（`model_context`）：超过它上游直接 400，所以是天花板；
      * **用户设置** `maxContextTokens`：想省额度的人自己调小（缺省 = 不表态）；
      * **实例上限** `cap`（`settings.ai_max_context_tokens`）：站长兜底。

    顺带它就是"换模型自动跟着变"的那一处：模型名一换，窗口跟着换，
    不用任何人手改数字（用户要的就是这个）。
    """
    window = model_context(model, cap)
    want = int(conf.get("maxContextTokens") or 0) or window
    return max(MIN_CONTEXT_TOKENS, min(want, window, cap))


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

    * **通道不是 beta**（正式包）／被显式关掉（`QF_AI_BETA_ENABLED=false`）／文件不在 → None
    * 文件按 **mtime** 缓存：改完立刻生效，不必重启
    * 坏文件当成"没有"而不是抛异常 —— 一个手写的 JSON 不该让整站 AI 全挂

    密钥只在这里落地，**不进 Settings**：那个对象会被 repr、被日志打印。

    判据是 `settings.beta_active` 而不是单个开关：**正式通道连文件都不看**，
    这样"内测和正式分开"是结构上的保证，而不是靠配置写对。
    """
    settings = get_settings()
    if not settings.beta_active:
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


def resolve_config(db) -> dict:  # noqa: ANN001
    """取出这次调用该用哪套配置：**本机设置里的密钥 > 内测通道 > 明确报错**。

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

    row = settings_row(db)
    conf = ((row.data if row else {}) or {}).get("ai") or {}

    api_key = str(conf.get("apiKey") or "").strip()
    timeout_ms = int(conf.get("timeoutMs") or DEFAULT_TIMEOUT_MS)
    timeout_ms = max(MIN_TIMEOUT_MS, min(timeout_ms, settings.ai_timeout_ms))

    if conf.get("enabled") and api_key:
        model = str(conf.get("model") or "").strip() or DEFAULT_MODEL
        return {
            "apiKey": api_key,
            "baseUrl": str(conf.get("baseUrl") or "").strip() or DEFAULT_BASE_URL,
            "model": model,
            "vision": _vision_of(conf, model),
            "timeoutMs": timeout_ms,
            # **按模型自动定**（见 `budget_tokens`）：模型窗口 / 用户设置 / 实例上限取小
            "maxContextTokens": budget_tokens(model, conf, settings.ai_max_context_tokens),
            # 上游支持 JSON 模式就带上：资料元数据那一问要的就是一段 JSON，
            # 有它就不用猜模型会不会裹一层"好的，这是结果"。
            "jsonMode": bool(conf.get("jsonMode")),
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
            # 内测通道用的是**它自己那份模型名**在算窗口（见 budget_tokens），
            # 而 `maxContextTokens` 仍取用户设置里那个（他照样能自己调小）
            "maxContextTokens": budget_tokens(model, conf, settings.ai_max_context_tokens),
            "jsonMode": bool(beta.get("jsonMode")),
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


def embeddings_endpoint(base_url: str) -> str:
    return base_url.rstrip("/") + "/embeddings"


#: 向量模型默认名。与 `pipeline/config.py` 的默认值保持一致 ——
#: **它指向一个当前 provider 并不提供的模型**，这正是那个外部阻塞
#: （见 `docs/检索与向量化.md` §1.4）。留着它是为了让"换一个能出向量的
#: provider"只改配置，不用改代码。
DEFAULT_EMBED_MODEL = "text-embedding-3-small"


class EmbeddingUnavailable(RuntimeError):
    """当前 provider 不提供 embedding（或它拒了这个模型名）。

    与 `UpstreamError` 分开，是因为**下一步动作完全不同**：连不上要查密钥与网络，
    而这个要换 provider 或换模型名。写成同一句，两种人都会白折腾一遍
    （与 `connection_hint` 那条同一个理由）。
    """


def embed_texts(conf: dict, texts: list[str], *, model: str = "") -> list[list[float]]:
    """向量化一批文本（运行时那一路）。

    为什么单独写一条而不复用 `stream_completion`：那是 SSE 流式聊天，而
    `/embeddings` 是**一次性 JSON**、返回体里连 `choices` 都没有 —— 硬塞进
    同一条路只会把它弄复杂。

    404 / 400 一律归成 `EmbeddingUnavailable`：这两者几乎总是"这个通道没有
    向量模型"或"模型名不对"，而不是网络问题。
    """
    if not texts:
        return []
    body = {"model": model or str(conf.get("embedModel") or "") or DEFAULT_EMBED_MODEL,
            "input": texts}
    headers = {
        "Authorization": "Bearer " + str(conf.get("apiKey") or ""),
        "Content-Type": "application/json",
    }
    timeout = httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)
    with httpx.Client(timeout=timeout) as client:
        response = client.post(
            embeddings_endpoint(str(conf.get("baseUrl") or "")), json=body, headers=headers
        )
    if response.status_code in (400, 404):
        detail = response.text[:200]
        raise EmbeddingUnavailable(
            "这个接口没有可用的向量模型（HTTP %d）：%s\n"
            "向量检索要一个能出向量的通道 —— 见 docs/检索与向量化.md §1.4。"
            % (response.status_code, detail)
        )
    if response.status_code >= 400:
        raise UpstreamError(
            "向量化失败：接口返回 %d" % response.status_code,
            kind="http",
            status=response.status_code,
            detail=response.text[:300],
        )
    items = (response.json() or {}).get("data") or []
    if not items:
        raise EmbeddingUnavailable("向量化返回为空（这个通道大概没有向量模型）。")
    return [list(item.get("embedding") or []) for item in items]


def today_usage(db) -> int:  # noqa: ANN001
    from datetime import date as date_type

    from sqlalchemy import select

    return (
        db.scalar(select(AiUsage.calls).where(AiUsage.date == date_type.today()))
        or 0
    )


def enforce_quota(db) -> None:  # noqa: ANN001
    """每日上限，0 表示不限。

    单机下它约束的是「这一天刷太多了」—— 挡住失控的重试循环，
    而不是"替别人兜额度"（那件事在自带密钥的模式下本来就不存在）。
    """
    from fastapi import HTTPException, status as http_status

    quota = get_settings().ai_daily_quota
    if quota and today_usage(db) >= quota:
        raise HTTPException(
            http_status.HTTP_429_TOO_MANY_REQUESTS,
            f"今日 AI 调用已达上限（{quota} 次），明天再来或改用「自己判断」。",
        )


def record_usage(  # noqa: ANN001
    db,
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
    并发发请求时，后者会丢计数。
    """
    from datetime import date as date_type

    from sqlalchemy import func
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    db.execute(
        sqlite_insert(AiUsage)
        .values(
            date=date_type.today(),
            calls=1,
            failures=0 if ok else 1,
            prompt_tokens=max(prompt_tokens, 0),
            completion_tokens=max(completion_tokens, 0),
            last_latency_ms=latency_ms,
            last_error="" if ok else detail[:300],
        )
        .on_conflict_do_update(
            index_elements=[AiUsage.date],
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
                    # 5xx 且上游一个字都没说 —— 那通常不是供应商在说话，而是路上的
                    # 代理/网关替它回的（实测：本机代理会把一个根本连不上的地址变成
                    # 空体 502）。这时归 `connect` 而不是 `http`：既补上该找谁的提示，
                    # 也让前端标成可重试（网关抖动是暂时的）—— 与非流式那条路同一判据。
                    if response.status_code >= 500 and not detail.strip():
                        raise UpstreamError(
                            connection_hint(conf)
                            + f"（网关回了 {response.status_code}，没有内容）",
                            kind="connect",
                            status=response.status_code,
                            detail=detail,
                        )
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
        # **一个字都没收到 = 够不着上游**，不是"上游慢" —— 这是最该给指点的一种失败。
        # 只说"没有任何响应"，走内测通道的人不知道该去找站长、自带密钥的人不知道
        # 该查地址与网络：`connection_hint` 就是为这半句写的。
        raise UpstreamError(
            connection_hint(conf) + f"（上游 {waited:.0f} 秒内没有任何响应，{why}）",
            kind="timeout",
        ) from None
    except httpx.HTTPError as exc:
        raise UpstreamError(
            connection_hint(conf) + "（" + str(exc)[:160] + "）", kind="connect"
        ) from None
