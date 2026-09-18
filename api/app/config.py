"""运行时配置。

全部从环境变量读取（前缀 ``QF_``），本地开发可写 ``api/.env``。

这里只有**实例级**的 AI 策略（总开关 / 每人日限 / 超时），没有任何人的密钥 ——
密钥属于各自账号的设置，存在库里，浏览器侧拿不到也不该拿到。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

API_DIR = Path(__file__).resolve().parent.parent
ROOT = API_DIR.parent


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

    # ------------------------------------------------------------ 数据
    # **local-first**：应用自己的东西（数据库，以后还有笔记与资料的索引）都落在这个
    # 数据目录里，不依赖任何外部服务 —— 双击即用，没有"先把数据库起起来"这一步。
    data_dir: Path = ROOT / "data"

    # ------------------------------------------------------------ 数据库
    # 留空 = 用 `data_dir` 下的 `quizforge.db`（SQLite，随应用分发）。
    # 想指到别处（内存库、临时文件）就设 `QF_DATABASE_URL`。
    database_url: str = ""
    db_echo: bool = False

    # 会话相关的配置项（cookie 名、有效期、令牌字节数）随账号面一起删掉了 ——
    # 单用户本地形态没有会话，见 `app/deps.py`。

    def model_post_init(self, __context: object) -> None:
        """没显式给连接串就用数据目录下的 SQLite 文件。

        放在这里而不是写成字段默认值：默认值要引用**另一个字段**（`data_dir`），
        而字段默认值在类定义时就求值了 —— 那样 `QF_DATA_DIR` 会被忽略。
        """
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
    # **每个用户用自己填的密钥**（存在各自的 user_settings.data.ai 里），
    # 服务端不持有、也不提供共享密钥 —— 谁的额度谁负责。
    # 这里只保留三项与个人凭据无关的策略。
    ai_enabled: bool = True  # 实例级总开关，关掉后所有人都不能用
    ai_timeout_ms: int = 90000  # 超时上限，用户不能把自己设得比这更长
    ai_daily_quota: int = 0  # 每人每天的批改次数上限（0 = 不限）
    # 对话的上下文预算上限（用户可设得更小，不能更大）——
    # 与超时同一条规矩：模型窗口是用户自己的事，但站长得有个兜底
    ai_max_context_tokens: int = 8000

    # ------------------------------------------------------- 内测通道
    # 内测期间让「没有自带密钥的人也能聊」：一个**实例级**的 OpenAI 兼容配置，
    # 写给内测用。它长什么样与用户自己那份（`user_settings.data.ai`）完全一致，
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
    ai_beta_enabled: bool = True
    ai_beta_config_file: Path = ROOT / "config" / "ai.local.json"

    # ------------------------------------------------------------ 导入器
    # 容器里用 QF_QUESTIONS_DIR / QF_TOPICS_FILE / QF_TOOLS_DIR 指到挂载点。
    # tools/ 是仓库根的脚本目录（题库解析器、主题树、校验器都在里面），
    # 导入器直接复用它们，所以必须能找到。
    questions_dir: Path = ROOT / "questions"
    topics_file: Path = ROOT / "meta" / "topics.yaml"
    tools_dir: Path = ROOT / "tools"

    # ------------------------------------------------------------ 派生属性
    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    @property
    def is_postgres(self) -> bool:
        """还在用 Postgres 吗 —— 只剩"把老数据搬过来"那一次脚本会用到它。"""
        return self.database_url.startswith("postgresql")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例。全部来自环境变量（容器里由 compose 的 .env 提供）。

    不读 `config/ai.local.json`：那是离线单页时代用来把密钥内联进 HTML 的本机文件，
    随离线形态一起淘汰了 —— 现在没有任何代码读它，服务端也不持有任何人的 AI 密钥。
    """
    return Settings()
