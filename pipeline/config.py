"""流水线配置：从 config/ai.local.json 读 LLM 端点（与仓库其它部分同构）。

原则（见 pipeline.md §7）：所有 LLM 能力走 API，不引推理框架；
provider 只是配置——换一家只改这个文件读到的三个字段。

密钥永不打印、永不进 git（config/ai.local.json 已在 .gitignore 里）。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / "config" / "ai.local.json"
DB_FILE = ROOT / "pipeline" / "state.sqlite3"
MAPS_DIR = ROOT / "maps"
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"


@dataclass(frozen=True)
class LLMConfig:
    base_url: str
    api_key: str
    model: str
    vision_model: str
    embed_model: str
    timeout_s: float
    concurrency: int
    max_concurrency: int
    max_attempts: int
    lease_s: float
    # 花钱的三个闸门：没有它们，一个 bug（比如自动重出成环）能在几分钟内把余额烧光。
    budget_yuan: float          # 本次运行的硬预算，超了 worker 立刻停
    rate_in_yuan: float         # 每百万输入 token 的价（只用于估算，填真实价更准）
    rate_out_yuan: float
    max_tasks_per_dispatch: int  # 一次派工最多新建多少任务，超过要 --force
    max_tasks_per_run: int       # 一次 worker 运行最多处理多少任务，0=不限

    def describe(self) -> str:
        return (
            f"base_url={self.base_url} model={self.model} "
            f"vision={self.vision_model} embed={self.embed_model} "
            f"concurrency={self.concurrency} api_key={'set' if self.api_key else 'MISSING'} "
            f"预算=¥{self.budget_yuan} 派工上限={self.max_tasks_per_dispatch} 单次运行上限={self.max_tasks_per_run}"
        )


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def load() -> LLMConfig:
    """读配置。缺失的字段用环境变量补，仍缺失则报错——不猜。"""
    raw: dict = {}
    if CONFIG_FILE.is_file():
        raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))

    api_key = _env("QF_API_KEY") or str(raw.get("apiKey") or "")
    if not api_key:
        raise SystemExit(
            f"缺少 API 密钥：{CONFIG_FILE} 里没有 apiKey，也没有 QF_API_KEY 环境变量"
        )

    timeout_ms = raw.get("timeoutMs") or raw.get("timeout_ms") or 120_000
    try:
        timeout_s = max(5.0, float(timeout_ms) / 1000.0)
    except (TypeError, ValueError):
        timeout_s = 120.0

    # 视觉与向量模型默认为同一个模型；可用配置字段或环境变量覆盖。
    # 换 provider / 换模型只改这里读到的值，代码不动。
    vision_model = (
        _env("QF_VISION_MODEL")
        or str(raw.get("visionModel") or "")
        or _env("QF_MODEL")
        or str(raw.get("model") or "")
    )
    embed_model = (
        _env("QF_EMBED_MODEL")
        or str(raw.get("embedModel") or "")
        or "text-embedding-3-small"
    )

    return LLMConfig(
        base_url=_env("QF_BASE_URL") or str(raw.get("baseUrl") or DEFAULT_BASE_URL).rstrip("/"),
        api_key=api_key,
        model=_env("QF_MODEL") or str(raw.get("model") or "deepseek-chat"),
        vision_model=vision_model,
        embed_model=embed_model,
        timeout_s=timeout_s,
        # 并发不是"每个 worker 几路"，而是**全局一道闸门**的起点与上限：
        # 起点 16 起步、上限 64，闸门自己按 429 反馈上下浮动（见 AdaptiveLimiter）。
        concurrency=int(_env("QF_CONCURRENCY", "16") or 16),
        # 上游（DeepSeek）的并发上限实测是 2500，所以限制不在它那一侧 ——
        # 真正卡住的是「同时存在多少个独立任务」：一个材料约 28 个，59 个材料 ≈ 1600。
        # 默认给 256（够用且稳妥），要更宽就 QF_MAX_CONCURRENCY=1024。
        max_concurrency=int(_env("QF_MAX_CONCURRENCY", "256") or 256),
        max_attempts=int(_env("QF_MAX_ATTEMPTS", "3") or 3),
        # 租约从 900 秒收到 120 秒：**长任务被中断是常态**（断网、Ctrl-C、被 kill），
        # 而租约是「进程死了以后多久把任务放回队列」的唯一兜底。实测一次调用的 P90 是
        # 53 秒（抽取最长的那些），120 秒足够覆盖正常调用，又让崩溃后的停摆从 15 分钟缩到 2 分钟。
        # 正常退出时还会主动释放（见 worker.run 的 finally），所以这个值只是最后的保险。
        lease_s=float(_env("QF_LEASE_S", "120") or 120),
        # 默认值刻意保守：一次派工最多 20 个任务、一次运行最多 20 个任务、预算 ¥2。
        # 要放量必须显式给 --force（派工）或调大这三个值 —— 默认不会烧钱。
        budget_yuan=float(_env("QF_BUDGET_YUAN", "2") or 2),
        rate_in_yuan=float(_env("QF_RATE_IN", "2") or 2),
        rate_out_yuan=float(_env("QF_RATE_OUT", "8") or 8),
        max_tasks_per_dispatch=int(_env("QF_MAX_DISPATCH", "20") or 20),
        max_tasks_per_run=int(_env("QF_MAX_RUN", "20") or 20),
    )
