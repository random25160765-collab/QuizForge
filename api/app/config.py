"""运行时配置。

全部从环境变量读取（前缀 ``QF_``），本地开发可写 ``api/.env``。

这里只有**实例级**的 AI 策略（总开关 / 每人日限 / 超时），没有任何人的密钥 ——
密钥属于各自账号的设置，存在库里，浏览器侧拿不到也不该拿到。
"""

from __future__ import annotations

import json
import os
import sys
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

API_DIR = Path(__file__).resolve().parent.parent
ROOT = API_DIR.parent


def _resource_root() -> Path:
    """随包分发的**只读资源**在哪：开发时是仓库根，打包后是解包目录。

    与"数据目录"是两回事：数据目录是可写的、属于用户的（`~/quizforge`）；
    这里的东西跟着包走，不该被改。

    打包后**千万不能用 `ROOT`**：单文件模式里它是解包目录的上一级（`%TEMP%`），
    于是"内测配置在不在""哈希清单在不在"会被问到一个临时目录里去 —— 这个坑刚踩过
    （`heavy_deps` 里也有一份同样的判断，两处都改过才算干净）。
    """
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", API_DIR))
    return ROOT


RESOURCE_ROOT = _resource_root()


#: 通道：`beta`（内测 —— 用**站长的额度**，给试用的人）或 `release`（正式 —— 用各人自己的密钥）。
#:
#: ① **分发的 exe 默认 beta**（用户定的：内测通道就是给人试用的）；
#: ② 两条通道的**数据目录不同**（`~/quizforge-beta` vs `~/quizforge`）—— 内测怎么折腾
#:    都碰不到正式那份库、记录、以后的笔记与资料；
#: ③ 正式通道**连内测配置文件都不看**，所以正式包里用不到站长的额度。
CHANNELS = ("beta", "release")


def _default_channel() -> str:
    """通道从哪来：环境变量 > 包里带的 `channel.json` > 开发机默认。

    `channel.json` 是打包时写进包里的（见 `build/package.py`）—— 冻结之后没有
    环境变量可读，只能把"这是哪个通道的包"写在一个跟着包走的小文件里。
    """
    raw = os.environ.get("QF_CHANNEL", "").strip().lower()
    if raw in CHANNELS:
        return raw
    bundled = RESOURCE_ROOT / "channel.json"
    if bundled.is_file():
        try:
            payload = json.loads(bundled.read_text(encoding="utf-8"))
            value = str((payload or {}).get("channel") or "").strip().lower()
        except ValueError:
            value = ""
        if value in CHANNELS:
            return value
    # 开发机默认内测：仓库里就放着 `config/ai.local.json`，日常开发一直这么走
    return "beta"


def frozen_data_dir(channel: str) -> Path:
    """打包后数据放哪：**用户主目录**下，且**按通道分开**。

    为什么不能沿用 `<根>/data`：单文件模式里那个"根"是解包出来的临时目录
    （`%TEMP%`），数据攒在那儿既不好找，又随时可能被清理工具当垃圾收走 ——
    用户的东西不该住在临时目录里。

    为什么按通道分：用户原话"内测和正式要分开"。同一台机器上跑内测包与正式包，
    库、作答记录、以后的笔记与资料互不干扰。
    """
    return Path.home() / ("quizforge-beta" if channel == "beta" else "quizforge")


def _default_tools_dir() -> Path:
    """`tools/` 在哪：跟着**只读资源**走（开发时是仓库根，打包后是解包目录）。

    为什么不干脆省掉它：**出题流水线要用它** —— `app/toolkit.py` 把
    `question_parser` 与 `check` 挂到 import 路径上，而 A 段的"资料即入库口"
    会让桌面应用去跑那条流水线。少了它，应用照样启动，但一点"送去做题"当场炸。
    """
    return RESOURCE_ROOT / "tools"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="QF_",
        env_file=str(API_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---------------------------------------------------------------- 基础
    app_name: str = "quizforge"
    debug: bool = True

    # 这里**刻意没有** secret_key / 签名密钥。
    # 会话令牌是 32 字节随机串（secrets.token_urlsafe），库里只存它的 sha256，
    # 校验时查表比对 —— 没有任何需要服务端持有密钥来做签名或验签的环节。
    # 早期从 JWT 方案沿袭下来的 QF_SECRET_KEY 是一条假需求：它从未被读取过，
    # 却让运维多做一个无用步骤、还以为不设会出错。宁可删掉也不要留误导。

    # ------------------------------------------------------------ 通道
    # `beta`（内测，用站长的额度）或 `release`（正式，用各人自己的密钥）。
    # 分发的 exe **默认 beta**；开发机也默认 beta（仓库里就放着内测配置）。
    # 它决定三件事：内测额度能不能用、打包后数据放哪个目录、以及界面上怎么自称。
    channel: str = _default_channel()

    # ------------------------------------------------------------ 数据
    # **local-first**：应用自己的东西（数据库，以后还有笔记与资料的索引）都落在这个
    # 数据目录里，不依赖任何外部服务 —— 双击即用，没有"先把数据库起起来"这一步。
    # 开发时是仓库里的 `data/`；打包后搬进主目录并按通道分开（见 `model_post_init`）。
    data_dir: Path = ROOT / "data"

    # ------------------------------------------------------------ 数据库
    # 留空 = 用 `data_dir` 下的 `quizforge.db`（SQLite，随应用分发）。
    # 想指到别处（内存库、临时文件）就设 `QF_DATABASE_URL`。
    database_url: str = ""
    db_echo: bool = False

    # ------------------------------------------------------ 重型运行组件
    # 首启在后台把 Pyodide（76M，不进安装包）取进 `data/cache/pyodide/`。
    # 关掉它 = 不主动联网，等真正用到「跑 Python」那一次再取（见 app/heavy_deps.py）。
    # 测试里一律关掉：用例不该因为跑一次就去下载几十兆。
    heavy_prefetch: bool = True

    # 会话相关的配置项（cookie 名、有效期、令牌字节数）随账号面一起删掉了 ——
    # 单用户本地形态没有会话，见 `app/deps.py`。

    def model_post_init(self, __context: object) -> None:
        """两件要在"所有字段都就位"之后才能定的事。

        放在这里而不是写成字段默认值：默认值要引用**别的字段**（`channel` / `data_dir`），
        而字段默认值在类定义时就求值了 —— 那样 `QF_DATA_DIR` 也会被忽略。
        """
        # ① 打包后：数据目录搬进用户主目录，并按通道分开。
        #    显式给了 `QF_DATA_DIR` 就不动（测试与打包自检都靠它指临时目录）。
        if getattr(sys, "frozen", False) and not os.environ.get("QF_DATA_DIR"):
            self.data_dir = frozen_data_dir(self.channel)

        # ② 没显式给连接串就用数据目录下的 SQLite 文件
        if not self.database_url:
            self.database_url = f"sqlite:///{self.data_dir / 'quizforge.db'}"

    # ------------------------------------------------------------ 静态资源
    # 由 tools/build.py --web 输出，FastAPI 直接挂载。
    # 默认相对 api/ 而不是仓库根：容器里代码可能不在仓库结构下，
    # 整个目录拷贝过去也能跑（容器里用 QF_WEB_DIR 覆盖）。
    web_dir: Path = API_DIR / "web"
    # 缓存策略写在 main.py 的 cache_policy 中间件里（字体长缓存、其余每次校验），
    # 不是配置项 —— 它是与「文件名不带内容哈希」这一事实绑定的，不该被随意调。

    # --------------------------------------------------------------- CORS
    # 开发时前端可能跑在别的端口（vite/静态服务器），用逗号分隔
    cors_origins: str = ""

    # ---------------------------------------------------------------- AI
    # **每个用户用自己填的密钥**（存在各自的 app_settings.data.ai 里），
    # 服务端不持有、也不提供共享密钥 —— 谁的额度谁负责。
    # 这里只保留三项与个人凭据无关的策略。
    ai_enabled: bool = True  # 实例级总开关，关掉后所有人都不能用
    ai_timeout_ms: int = 90000  # 超时上限，用户不能把自己设得比这更长
    ai_daily_quota: int = 0  # 每人每天的批改次数上限（0 = 不限）
    # 对话的上下文预算上限（用户可设得更小，不能更大）——
    # 与超时同一条规矩：模型窗口是用户自己的事，但站长得有个兜底。
    #
    # 8000 → 1_000_000：8000 那会儿太小了，一顿工具调用（学习模式光工具说明书
    # 就 5264 token）就把早期对话挤出去了，用户看到的是一句"更早的 N 条消息
    # 没有带进来"（那句话现已删除）。现在按"模型自己声明能吃多少"来定 ——
    # 见 `ai_gateway.model_context_tokens`；这里只是**兜底上限**，
    # 实际值取"模型窗口"与"用户设置"里更小的那个。
    ai_max_context_tokens: int = 1000000

    # ------------------------------------------------------- 内测通道
    # 内测期间让「没有自带密钥的人也能聊」：一个**实例级**的 OpenAI 兼容配置，
    # 写给内测用。它长什么样与用户自己那份（`app_settings.data.ai`）完全一致，
    # 密钥放在一个被 gitignore 的文件里：
    #
    #     config/ai.local.json  →  {"enabled":true,"baseUrl":"…","model":"…","apiKey":"…"}
    #
    # **这里只存路径，不存密钥**：Settings 会被 repr、会被日志打印，
    # 而密钥不该出现在任何一处输出里。文件由网关按 mtime 懒读，改完立刻生效。
    #
    # 它与上面那条「不替别人保管计费凭据」的关系要说清：内测通道是**故意**的例外，
    # 就是站长拿自己的额度请大家试用。所以：
    #   * 用户自己填了密钥 → 用他的（谁的额度谁负责，这条没变）
    #   * 没填 → 走内测通道，并且**建议把 QF_AI_DAILY_QUOTA 设成非 0**，
    #     否则一个人就能把这份共享额度刷光
    #   * 两条路都没有 → 明确报错，不静默降级
    #
    # 与「通道」的关系（2026-09-18 定）：**只有 beta 通道才读它**。
    # 正式包里连这个文件都不看，所以正式包用不到站长的额度（见 `beta_active`）。
    ai_beta_enabled: bool = True
    # 走 `RESOURCE_ROOT` 而不是 `ROOT`：打包后后者是解包目录的上一级（`%TEMP%`），
    # 会把"内测配置在不在"问到一个临时目录里去。打包时这个文件被放进包里
    # （`build/package.py --channel beta`），所以冻结之后它是**跟着包走**的。
    ai_beta_config_file: Path = RESOURCE_ROOT / "config" / "ai.local.json"

    # ------------------------------------------------------------ 导入器
    # 容器里用 QF_QUESTIONS_DIR / QF_TOPICS_FILE / QF_TOOLS_DIR 指到挂载点。
    # tools/ 是脚本目录（题库解析器、主题树、校验器都在里面），
    # 导入器与出题流水线直接复用它们，所以必须能找到。
    questions_dir: Path = ROOT / "questions"
    topics_file: Path = ROOT / "meta" / "topics.yaml"
    tools_dir: Path = _default_tools_dir()

    # ------------------------------------------------------------ 派生属性
    @property
    def beta_active(self) -> bool:
        """内测额度还能不能用：**通道是 beta**，且没被显式关掉。

        这是"内测和正式要分开"的落点：正式通道下**连文件都不看**，
        所以正式包不可能用到站长的额度 —— 而不是"看着像没配、其实能命中"。
        """
        return self.channel == "beta" and self.ai_beta_enabled

    @property
    def is_beta(self) -> bool:
        return self.channel == "beta"

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    @property
    def is_postgres(self) -> bool:
        """还在用 Postgres 吗 —— 只剩"把老数据搬过来"那一次脚本会用到它。"""
        return self.database_url.startswith("postgresql")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例。全部来自环境变量（开发时由 `api/.env` 提供）。

    `Settings` 自己**不装密钥**（它会被 repr、被日志打印）—— 内测配置只留一个**路径**
    在这里，真读它的是 `ai_gateway.beta_config()`，而且只在 beta 通道下读。
    """
    return Settings()
