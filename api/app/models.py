"""数据模型。

## 题目为什么用「提升列 + payload JSONB」

题的权威表示就是 `tools/question_parser.py` 产出的那个 dict，
前端消费的也是同一份 JSON。若把它拆成
options / answer / parts 等若干张表，就多出一层「库表 ↔ 解析器」
映射要维护，任何一侧改了字段都会静默漂移。

所以这里：``payload`` 存**原样 JSON**（与文件构建路径逐字节一致），
同时把真正需要服务端查询的字段（type / topic / difficulty）提升为列并建索引。
既保真，又能按维度筛选与聚合。

## 为什么要 retired_at

主题树或题库都会变。删掉一个知识点 / 一道题时，用户的做题记录不能跟着消失，
否则掌握度与历史统计会出现空洞。所以删除是「标记退役」而不是物理删除，
历史记录仍能关联到已退役的行。
"""

from __future__ import annotations

import uuid
from datetime import UTC
from datetime import date as date_type
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    LargeBinary,
    SmallInteger,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class UtcDateTime(TypeDecorator):
    """总是**按 UTC 存、按 UTC 还原**的 datetime。

    为什么需要它：SQLite 没有时区类型，`UtcDateTime` 落库时把偏移丢了 ——
    读回来是 **naive** 的，而 naive datetime 的 `.timestamp()` 会按**本地时区**解释。
    换内置引擎时实测就撞在这上面：UTC+8 的机器上，作答时间读回来整整差 8 小时，
    「更早的时间要更新首次作答」那条用例挂在它上面（`firstAt` 差 28800000 毫秒）。

    存进去时统一转 UTC；读出来时若没有时区信息就**补上 UTC**。
    这样 `datetime.timestamp()` 在任何机器上都得到同一个绝对值。
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:  # noqa: ANN001
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:  # noqa: ANN001
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

# Postgres 上用 JSONB（可索引、可查询）；其它方言退化为普通 JSON，
# 代价为零，好处是测试可以直接跑在 SQLite 上。
JSONType = JSON().with_variant(JSONB(), "postgresql")

#: 自增大整数主键。
#: SQLite 只把 **`INTEGER PRIMARY KEY`** 当 rowid 别名（也就是只有它才会自增），
#: `BIGINT PRIMARY KEY` 不行 —— 换内置引擎时实测撞上「NOT NULL constraint failed:
#: messages.id」：插入时没给 id、数据库也不替它生成。
#: 所以这一列在 SQLite 上退化成 Integer（SQLite 的整数本来就是 64 位，装得下）。
BigAutoId = BigInteger().with_variant(Integer, "sqlite")


#: `users` 表与 `User` 模型已在 2026-09-22 **整体删除** —— 连同 10 张表上的
#: `user_id` 外键（见 `docs/STATUS.md`）。
#:
#: 它是多用户 SaaS 时代的产物。2026-09-18 那次"去账号"只删掉了**登录面**
#: （注册 / 登录 / 会话 / CSRF），却把 `users` 表与 `user_id` 留着，理由是
#: "那是将来多设备同步最自然的接口"。那条理由不成立：这是个 **local-first 单机**
#: 应用（见 `docs/THESIS.md`），单机里"用户"这个概念本身就没有指代 ——
#: 没有第二个人，也就没有"归属"要记。留着它的真实代价是：`password_hash`
#: 这种毫无意义的列一直躺在库里，而每个新功能都要先想一遍"这个 user_id 怎么办"。
#:
#: 唯一的代价是：将来真要做多设备同步，得另设一个归属键。
#: 到那时再加，比现在假装需要它更诚实。


class BankVersion(Base):
    """一次题库导入的快照记录，用于 ETag 协商缓存与回滚定位。"""

    __tablename__ = "bank_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    content_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    imported_at: Mapped[datetime] = mapped_column(UtcDateTime, server_default=func.now(), nullable=False)
    question_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    topic_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    stats: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Topic(Base):
    """考纲主题树的一级展开（主题 → 单元 → 知识点）。

    层级字段（path / children / descendants）在导入时由 `tools/topics.py`
    算好后直接落库，前端拿到即可用，不必再爬树。
    """

    __tablename__ = "topics"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    group_key: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    order_index: Mapped[int] = mapped_column(Integer, default=999, nullable=False)
    color: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    desc: Mapped[str] = mapped_column(Text, default="", nullable=False)

    depth: Mapped[int] = mapped_column(SmallInteger, default=1, nullable=False)
    parent_key: Mapped[str | None] = mapped_column(String(64), index=True)
    path: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    path_names: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    children: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    descendants: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    is_leaf: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # 「主题」这个维度也放在 topics 里（depth=1 的行）：
    # import 时正常 upsert，退役时置时间戳
    retired_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Question(Base):
    """题目。**数据库是唯一权威** —— 不再一题一个文件。

    为什么改：一题一个 `.md` 在 10 本书的规模下就是几十万个小文件，
    既管理不动、也没法和 LLM 对齐（模型输出得先落盘、再搬运、再解析一次）。
    现在模型输出**直接写这张表**，草稿/已校验/已发布只是 `status` 的三个取值。

    状态机：``draft``（出题员刚写完）→ ``verified``（独立校验通过）→ ``published``（可对外）
    ；校验不通过则留在 draft 并记 ``verify_report``；下架走 ``retired``（复用 retired_at）。
    """

    __tablename__ = "questions"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    type: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    topic: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    difficulty: Mapped[int] = mapped_column(SmallInteger, default=3, index=True, nullable=False)
    source: Mapped[str] = mapped_column(Text, default="", nullable=False)

    # draft / verified / published（retired 见 retired_at）
    # server_default 是给**存量行**用的：109 道已入库的题一律视作已发布
    status: Mapped[str] = mapped_column(
        String(16), default="draft", server_default="published", index=True, nullable=False
    )
    layer: Mapped[str] = mapped_column(String(8), default="", server_default="", index=True, nullable=False)
    wing: Mapped[str] = mapped_column(String(8), default="", server_default="", nullable=False)
    chapter: Mapped[str] = mapped_column(String(128), default="", server_default="", nullable=False)
    # 校验员的判定原文（通过或退回的理由），出题质量分析要看它
    verify_report: Mapped[dict | None] = mapped_column(JSONType)

    # **原始的 Markdown 正文（front-matter 之下的部分）**：判分与渲染都用解析后的 payload，
    # 但"原文"必须留着 —— 导出、契约改动后重新解析，全靠它。
    # 没有这一列，库就只能算半个权威（丢不掉文件）。
    raw_markdown: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    # 与 question_to_dict() 完全一致的权威 JSON
    payload: Mapped[dict] = mapped_column(JSONType, nullable=False)
    # 判重与「变了没有」都看它（正文哈希，与 id 无关）
    content_hash: Mapped[str] = mapped_column(String(64), index=True, nullable=False)

    bank_version_id: Mapped[int | None] = mapped_column(ForeignKey("bank_versions.id", ondelete="SET NULL"))
    retired_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_questions_topic_type", "topic", "type"),
        Index("ix_questions_status_layer", "status", "layer"),
    )


class MyQuestion(Base):
    """我的题单里的一道题 —— **自己出的**，与公共题库分开。

    ## 为什么另开一张表，不塞进 `questions`

    `questions` 是公共题库的权威表：图谱（`question_concepts`）、覆盖率对账、
    出题流水线的状态机（draft → verified → published）全挂在它上面。
    用户随手出的题混进去有三个问题：污染统计口径（"覆盖率"里混进刚生成的一题）、
    让流水线去管它、以及**删不掉**（公共表的题只下架、不删除）。

    ## 与公共题的关系

    `payload` 与 `questions.payload` **同构**（stem / options / answer / explanation /
    parts …），于是渲染、判分、练习、组卷这些代码不必分叉 ——
    差别只在"题从哪来"那一处。`point_key` 记它冲着哪个知识点出的（可空），
    `conversation_id` 记它是哪条对话里出的（可空，练习页也能手写一道）。
    """

    __tablename__ = "my_questions"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSONType, nullable=False)
    # 由哪条对话出的。刻意**不加外键**：对话删了不该连带影响题单，
    # 而这个字段只用来显示"来自哪次对话"。
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    point_key: Mapped[str] = mapped_column(
        String(96), default="", server_default="", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_my_questions_created", "created_at"),)


class PointCandidate(Base):
    """抽取员的原始产出（每个切片一条一条候选点）。

    以前这些落成 `maps/<材料>/extract/sl-00N.json` —— 一个材料几十个文件，
    几十份材料就是几千个。现在直接入库：归并员从这里读，归并结果写 `knowledge_points`。

    保留 `raw` 原样 JSON 是关键：归并被改进后要能**重跑归并而不重跑抽取**
    （抽取是花钱的那一步，归并只是一次判断）。
    """

    __tablename__ = "point_candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    material_id: Mapped[int] = mapped_column(
        ForeignKey("materials.id", ondelete="CASCADE"), index=True, nullable=False
    )
    slice_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    key: Mapped[str] = mapped_column(String(160), index=True, nullable=False)
    name: Mapped[str] = mapped_column(Text, default="", nullable=False)
    kind: Mapped[str] = mapped_column(String(24), default="noun", nullable=False)
    thickness: Mapped[int] = mapped_column(SmallInteger, default=1, nullable=False)
    layers: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    sources: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    terms: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    raw: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    # 归并是否已经消化过它（重跑归并时置回 false 即可）
    consumed: Mapped[bool] = mapped_column(Boolean, default=False, index=True, nullable=False)

    __table_args__ = (UniqueConstraint("material_id", "slice_id", "key", name="uq_candidate"),)


class Record(Base):
    """一道题的累计学习状态。

    ## 字段被刻意劈成两半

    **计数**（`attempts / correct / partial / wrong / first_at / last_at /
    last_status / last_score / last_response`）只由 `attempts` 流水增量维护，
    **永远不接受客户端上传**。原因：客户端上传的是「它本地看到的累计值」——
    前端有一份乐观缓存（localStorage），离线做了一阵之后那份是旧的，
    按 last-write-wins 覆盖会让这段作答**静默丢掉**（计数不增、用户毫无察觉）。
    流水是增量、插入是精确一次的，相加天然正确。

    **补丁**（`patch` 里的 sm2 / note / streak）与 **动作**
    （`mastered` / `flagged`）是用户的主观点击，没有可加和的语义，
    照旧按 `client_rev` 做 last-write-wins。

    这条边界要在服务端做成硬约束：升级用的 `patches` 里若混入计数字段，
    一律忽略（见 `sync_ops.apply_patches`）。
    """

    __tablename__ = "records"

    question_id: Mapped[str] = mapped_column(String(96), primary_key=True)

    # ---- 计数：只由 attempts 增量维护 ----
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    correct: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    partial: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    wrong: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # epoch 毫秒：与前端一致，避免时区来回转换
    first_at: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    last_at: Mapped[int] = mapped_column(BigInteger, default=0, index=True, nullable=False)
    last_status: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    last_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    last_response: Mapped[dict | list | None] = mapped_column(JSONType)

    # ---- 补丁：客户端主观状态，按 client_rev 的 last-write-wins ----
    patch: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)

    # ---- 用户动作 ----
    mastered: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    flagged: Mapped[bool] = mapped_column(Boolean, default=False, index=True, nullable=False)

    client_rev: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_records_updated", "updated_at"),)


class Attempt(Base):
    """逐次作答的流水 —— 计数的**唯一来源**。

    1. `id` 由客户端生成，主键约束让网络重试天然幂等（`ON CONFLICT DO NOTHING`）；
    2. 服务端只对**真正插入成功**的流水累加计数与每日统计，
       所以「请求发出但响应丢了、客户端重发」不会重复计数 ——
       弱网下这是最关键的一条保证；
    3. 逐次流水保留下来，日后聚合口径若改动，可以重算历史而无需用户重做题目。
    """

    __tablename__ = "attempts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    question_id: Mapped[str] = mapped_column(String(96), index=True, nullable=False)
    at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    # 归属哪一天由**客户端的本地日期**决定，随流水一起上报。
    # 不用服务端时区从 at 反推：服务器跑 UTC 时，用户凌晨作答会被算到前一天，
    # 热力图与「今日已练」就会对不上。
    day: Mapped[date_type] = mapped_column(Date, index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    response: Mapped[dict | list | None] = mapped_column(JSONType)
    # 作答时的主题快照：即使主题树后续调整，历史热力图仍能按当时的归属统计
    topic_key: Mapped[str] = mapped_column(String(64), default="", index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, server_default=func.now(), nullable=False)

    __table_args__ = (Index("ix_attempts_at", "at"),)


class DayStat(Base):
    """每日聚合，供热力图与趋势使用。

    topics 里存「一级主题 → 次数」，热力图按主题筛选就靠它。
    """

    __tablename__ = "days"

    date: Mapped[date_type] = mapped_column(Date, primary_key=True)
    answers: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    correct: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    topics: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)


class AppSettings(Base):
    """本机设置 + 选题篮 + 置顶主题，整体存一个 JSON —— **全表只有一行**。

    这些字段的演进速度远快于学习数据，逐字段建列只会让每次加设置都要写迁移。

    主键是那个恒为 1 的 `id`：单机下"设置"本来就是单例，而 SQLite 的表总得有个主键。
    取用一律走 `app/settings_store.py`（调用方看不见主键）。
    """

    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1, autoincrement=False)
    data: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    client_rev: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Material(Base):
    """知识空间：一份材料（一个源文件）。

    为什么不再用文件管理：题目与知识点一多，靠 `maps/` 目录和 YAML 既选不动、
    也查不动。文件降级为**导入导出格式**（可重建、可审计），检索一律走库。
    """

    __tablename__ = "materials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(160), unique=True, index=True, nullable=False)
    subject: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    title: Mapped[str] = mapped_column(Text, default="", nullable=False)
    source_path: Mapped[str] = mapped_column(Text, default="", nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    lines: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: 这份资料走到哪一步：`出题`（**信任语料** —— 抽候选点 / 归知识点 / 归概念 /
    #: 出题 / 进图谱）还是 `检索`（**待查语料** —— **只到切片与向量为止**）。
    #:
    #: **它是按件定的，不是按来源定的**：资料库里的东西也可以标成 `出题`。
    #: （用户原话："有的资料值得出题，有的资料看看就好，同样都是资料管理器里的资料，
    #: 它们需要的工艺深度是完全不一样的"。）
    #: 所以"两个库两种待遇"是错的框架 —— 是**一条链 + 一个开关**。
    #:
    #: 默认 `检索`：**"值得出题"是个判断，不该是默认**。写入口各自显式指定
    #: （`pipeline.dbsync` 给出题链的入口 → `出题`；`pipeline.intake` → `检索`）。
    depth: Mapped[str] = mapped_column(String(16), default="检索", index=True, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Concept(Base):
    """知识图谱的**概念层**：跨材料唯一的那个"东西"。

    为什么必须存在这一层：原先只有 `knowledge_points`（点），而点是
    **material-scoped** 的 —— `tt-metal-circular-buffer` 在 10 份材料里就是 10 个
    互不相识的点。于是：

    * 主题树只能逐个点镜像出叶子（tt-metal 下 443 片），因为没有任何东西
      能把"同一件事的十次出现"收成一个节点；
    * 题目只能绑到"某份材料的某一段"，一千多道题看着就是一本材料目录；
    * 覆盖率、掌握度分母虚高（同一概念被算了十次）。

    所以：**点 = 某个概念在某份材料里的一次出现**，概念才是图谱的节点。
    """

    __tablename__ = "concepts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(160), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(Text, default="", nullable=False)
    kind: Mapped[str] = mapped_column(String(24), default="noun", index=True, nullable=False)
    definition: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: 挂到考纲的哪个节点；自动推导可能为空（待归类），人可改
    topic_key: Mapped[str] = mapped_column(String(160), default="", index=True, nullable=False)
    #: 同一概念的其它写法（归并时记下来，用来解释"为什么并成一件事"）
    aliases: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    point_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    material_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    question_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: auto（程序归并）/ confirmed（人确认过）/ retired（并错了或不要了）
    status: Mapped[str] = mapped_column(String(16), default="auto", index=True, nullable=False)
    #: 归并的把握度 0~1，越低越该人看一眼
    confidence: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)

    #: 「以概念为中心」的判边问过它了吗（见 `pipeline.graph_build relate-centric`）。
    #: 用途只有一个：**别把同一个概念反复问** —— 模型说"没有前置"的也要留痕，
    #: 否则下次查询照样选中它，钱白花。
    centric_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ConceptEdge(Base):
    """概念之间的关系 —— 这才是"知识图谱"里那个**图**。

    与 `point_edges` 的区别：那张表里的边（同切片 / 共享术语）本质是**相似度**，
    是程序按字面算出来的聚类；这里存的是**语义关系**（前置、组成、对比、实现），
    从邻近的一对对概念里由模型提出、可被人改。两者都留着：前者当证据，后者当知识。

    受控词表（不要自由加，前端按它配色）：
      requires      A 是 B 的前置（学 B 之前得先会 A）
      part_of       A 是 B 的组成部分
      contrast_with A 与 B 易混，需要辨析
      implements    A 是 B 的一种实现 / API 对应
      co_occurs     A 与 B 反复同时出现（程序派生，弱边，默认不显示）
    """

    __tablename__ = "concept_edges"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    from_concept_id: Mapped[int] = mapped_column(
        ForeignKey("concepts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    to_concept_id: Mapped[int] = mapped_column(
        ForeignKey("concepts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    type: Mapped[str] = mapped_column(String(24), index=True, nullable=False)
    #: 为什么这么连（模型给的一句话，或程序写的计数）—— 图上点开就能看到依据
    why: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: code | llm | human
    derived_by: Mapped[str] = mapped_column(String(16), default="code", nullable=False)
    weight: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("from_concept_id", "to_concept_id", "type", name="uq_concept_edge"),
    )


class QuestionConcept(Base):
    """题 ↔ **概念**：图谱与题库之间唯一该被算法使用的那条边。

    为什么不能只用 `question_points`：点是"某份材料里的一次出现"，而同一个概念
    在 10 份材料里就是 10 个点 —— 按点算覆盖率，一个概念被算十次，
    分母虚高十倍（`tt-metal-circular-buffer` 那种 10 点/10 材料的概念最典型）。

    所以：**点保留证据，概念上做统计**。覆盖率、选题、掌握度、复习计划
    一律走这张表；`question_points` 退回去当"出处"，仍然被出题与校验使用。

    旧题也补了这一列（`pipeline.graph_build merge` 会回填）——
    不然图谱只是好看，算不了东西。
    """

    __tablename__ = "question_concepts"

    question_id: Mapped[str] = mapped_column(
        String(96), ForeignKey("questions.id", ondelete="CASCADE"), primary_key=True
    )
    concept_id: Mapped[int] = mapped_column(
        ForeignKey("concepts.id", ondelete="CASCADE"), primary_key=True
    )
    is_primary: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class KnowledgePoint(Base):
    """知识空间：一个可考知识点（claim 簇）。

    **出题的原料单位是它，不是切片**：一个点常常横跨好几段原文，
    所以出处是一对多（`point_sources`），而不是一个行区间字段。

    它同时是**概念在某份材料里的一次出现**（`concept_id`）：图上的节点是概念，
    点是"这个说法在这份材料里出现过"的证据。归并没做的老数据 `concept_id` 为空，
    `pipeline.graph_build` 会补上。
    """

    __tablename__ = "knowledge_points"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    material_id: Mapped[int] = mapped_column(
        ForeignKey("materials.id", ondelete="CASCADE"), index=True, nullable=False
    )
    concept_id: Mapped[int | None] = mapped_column(
        ForeignKey("concepts.id", ondelete="SET NULL"), index=True, nullable=True
    )
    key: Mapped[str] = mapped_column(String(160), index=True, nullable=False)
    name: Mapped[str] = mapped_column(Text, default="", nullable=False)
    kind: Mapped[str] = mapped_column(String(24), default="noun", nullable=False)
    thickness: Mapped[int] = mapped_column(SmallInteger, default=1, nullable=False)
    layers: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    # 可考性验收：撑不起一道题的点进不了出题池
    producible: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)

    __table_args__ = (
        UniqueConstraint("material_id", "key", name="uq_point_material_key"),
        Index("ix_points_kind_layers", "kind", "thickness"),
    )


class PointSource(Base):
    """出处：点 → 原文行区间（可多段）。

    整个流水线的地基 —— 校验员只认它，出题员的依据也要落到它上面。
    """

    __tablename__ = "point_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    point_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_points.id", ondelete="CASCADE"), index=True, nullable=False
    )
    slice_id: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    start_line: Mapped[int] = mapped_column(Integer, nullable=False)
    end_line: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (UniqueConstraint("point_id", "start_line", "end_line", name="uq_point_range"),)


class PointEdge(Base):
    """点与点之间的**带类型边**（图就在这里，不需要单独的图数据库）。

    类型刻意只有几种，而且大部分能确定性派生：
      requires      前置（要考它得先会那个）—— 需要 LLM 判一次
      same_section  同一节（切片交集推出）
      shares_terms  共享术语 ≥ k（术语交集推出）
      part_of       拆分关系（归并阶段的动作推出）
      figure_of     图与它所画的那个点
    """

    __tablename__ = "point_edges"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    from_point_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_points.id", ondelete="CASCADE"), index=True, nullable=False
    )
    to_point_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_points.id", ondelete="CASCADE"), index=True, nullable=False
    )
    type: Mapped[str] = mapped_column(String(24), index=True, nullable=False)
    why: Mapped[str] = mapped_column(Text, default="", nullable=False)
    derived_by: Mapped[str] = mapped_column(String(24), default="code", nullable=False)

    __table_args__ = (
        UniqueConstraint("from_point_id", "to_point_id", "type", name="uq_edge"),
    )


class QuestionPoint(Base):
    """题 ↔ 点。覆盖率对账、按点重出、按点组卷都靠它。

    这也是「题目空间」与「知识空间」之间**唯一**的边 —— 有了它，
    "这个点有没有题""这道题覆盖哪些点"才是查询，而不是靠 topic 名字猜。
    """

    __tablename__ = "question_points"

    question_id: Mapped[str] = mapped_column(
        String(96), ForeignKey("questions.id", ondelete="CASCADE"), primary_key=True
    )
    point_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_points.id", ondelete="CASCADE"), primary_key=True
    )
    is_primary: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class MaterialSlice(Base):
    """材料的切片（原先只存在于 `maps/<材料>/slices.yaml`）。

    切片是**抽取的作业单位**：一个切片一次抽取调用，抽出来的候选点都挂在它上面。
    它必须进库的理由和考纲一样 —— 派工要问"哪些切片还没抽过"，
    而这个问题在几十个材料、上千个切片之后，靠扫文件不成立。
    """

    __tablename__ = "material_slices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    material_id: Mapped[int] = mapped_column(ForeignKey("materials.id"), nullable=False)
    slice_id: Mapped[str] = mapped_column(String(64), nullable=False)
    path: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    start_line: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    end_line: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tokens: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    figures: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: 切片**正文**（2026-11-08 加）：原先只有行区间指针，正文留在原文件里；
    #: 用户要"换一台机器也能访问材料库"，所以正文必须跟着切片走。
    #: 由 `make library-texts` 从原文件按行区间截取填入（切片覆盖 625333 行 ≈ 全文）。
    text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: 这一片**该有几个窗口**（向量化时写进来；0 = 还不知道）。
    #:
    #: 它是给**对账**用的缓存，不是权威：真正的答案是拿正文重算（`pipeline/ops.py` 的
    #: `--exact`）。为什么值得存一份：`slice_embeddings` 里"存了几行"只说明进度，
    #: 要判断"刻完了没有"必须知道分母，而分母要读材料正文才算得出来 —— 全库上百份
    #: 材料每次读一遍是几十秒，运维命令不该那么慢。
    windows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __table_args__ = (UniqueConstraint("material_id", "slice_id", name="uq_slice_material"),)


class LibraryTextCache(Base):
    """资料正文块的**本机缓存**（2026-11-09：手机不整包落 31.7MB 正文）。

    `library_cloud` 把正文按 768KB 一块写进云端 `qf_library_text`。桌面端整包拉下来
    回填 `material_slices.text`；开了 `lazy_bodies` 的机器只同步条目与切片坐标，
    点开某片时才取它所在的那一块，命中过的块留在这里。

    **它不是数据源**：只是正文的复印件，删掉只会让下次再取一次。按字节预算 LRU 淘汰
    （见 `app/slice_cloud.py`），所以不参与任何同步 / 数据包。
    """

    __tablename__ = "library_text_cache"

    #: 云端 `qf_library_text.idx` —— 一块一行，天然幂等
    idx: Mapped[int] = mapped_column(Integer, primary_key=True)
    bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    payload: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: 最后一次用到它的时刻（淘汰按它升序）
    at: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)


class SliceEmbedding(Base):
    """切片内**一个窗口**的向量 —— 检索层的那一路（`docs/检索与向量化.md`）。

    ## 为什么单位是「窗口」而不是「切片」

    §3.1 那条"粒度直接用切片，不另切一遍"**对 embedding 是错的**，实测：

    * 切片平均 **6979 字**、最长 36846 字；
    * 而模型的可用窗口是 **1024 token**（约 2000~3000 字）—— 一条切片一条向量，
      意味着**只嵌进了开头一小段**；
    * 于是「前言」那种短切片能被**完整**嵌入 → **完整赢过残缺** →
      所有中文问句都返回「前言」（实测：`环形缓冲区` 0.854 命中前言，真命中在 0.773）。

    所以嵌入单位是**窗口**（切片内按行切、带重叠），**窗口仍带自己的行区间** ——
    §3.3 那条"出处只认行区间"照样不破，而且比整片更精确。

    ## 它是派生产物，不是权威

    删了能重建（`python -m pipeline.embed`）。**向量只说"像不像"，绝不说"在哪一行"**；
    一旦让向量去"生成"出处，引用就不能再当证据了。

    ## 为什么存 BLOB 而不引向量库

    千级窗口 × 1024 维 float32 ≈ 几 MB，暴力算余弦是几十毫秒的事；专用向量库会
    引入一个和 SQLite 不同步的新服务 —— 与 `notelib.py` 拒绝倒排索引、
    与"双击即用"是同一条理由。也**不用 JSON 存**：那会放大三到四倍体积。
    """

    __tablename__ = "slice_embeddings"

    id: Mapped[int] = mapped_column(BigAutoId, primary_key=True, autoincrement=True)
    slice_id: Mapped[int] = mapped_column(ForeignKey("material_slices.id"), nullable=False)
    #: 片内第几窗（从 1 起）。重建时按它整片替换，不会留下孤儿行。
    ordinal: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    #: 这个窗口自己的行区间（切片之内）—— **出处就是它**
    start_line: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    end_line: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: 产出它的模型名。换模型 = 整批重算（判据就是这个列，见 `pipeline/embed.py`）
    model: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    dim: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: float32 紧凑打包（`semantic.pack`）
    vec: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    #: 被向量化的那段正文的哈希：**没改就不重算**（改了才重算，见 embed.py）
    text_hash: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=lambda: datetime.now(UTC), nullable=False
    )

    __table_args__ = (UniqueConstraint("slice_id", "ordinal", name="uq_embedding_window"),)


class EmbedRun(Base):
    """一次**向量化的跑单** —— 运维与观测要看的头一样东西。

    ## 为什么它必须进库

    向量化是这条流水线上唯一"分钟到小时级"的一步，而它以前在库里**一点痕迹都没有**：
    谁在跑、跑了多久、写到第几窗、是正常结束还是半途没了，只能靠 `ps` 加临时日志猜。
    实测为此吃了两次亏，都记在这儿：

    * 一个进程**退出码 0**，其实只写了每片的最后一窗（因为它是被上一次中断留下的半片）；
    * 另一个还在跑，而"每片至少有一行"被当成"算完了"，于是报了两次"齐了"。

    有了这张表，"正在跑"就是一条 `status='running'` 的行，配上 `pid` 还活着没有 ——
    中断能被看出来（进程没了、行还挂着），进度是 `windows_written / windows_planned`
    （**分母在开跑时就定下**，不是猜的），速率与 ETA 也从它算。

    ## 它只记事实，不做调度

    调度在 `pipeline/ops.py`（`drive` 子命令）。这张表是**账**，谁都能读（界面、
    运维命令、将来的定时任务），写它的只有一个地方：`pipeline/embed.py` 开跑与收尾。
    """

    __tablename__ = "embed_runs"

    id: Mapped[int] = mapped_column(BigAutoId, primary_key=True, autoincrement=True)
    #: 材料的 slug；`(全部)` 表示这一跑没限定材料。
    material: Mapped[str] = mapped_column(String(120), default="", nullable=False, index=True)
    batch: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pid: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    host: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    #: **跑在哪块芯片上** —— 开跑时还不知道，第一批算完写上（就是 ORT 报的实际 provider，
    #: 例如 `CUDAExecutionProvider` / `CPUExecutionProvider`）。不记这个的话，
    #: "这条跑单到底用没用上 GPU"只能靠猜，而 GPU 这条路最典型的事故正是静默退回 CPU。
    device: Mapped[str] = mapped_column(String(48), default="", nullable=False)
    #: `running` / `ok` / `failed`。中断是**外面看出来的**：`running` 而 pid 不在。
    status: Mapped[str] = mapped_column(String(16), default="running", nullable=False, index=True)
    #: 打算算多少片 / 多少窗（开跑定下），以及已经写进去多少窗。
    slices_planned: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    windows_planned: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    windows_written: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=lambda: datetime.now(UTC), nullable=False
    )
    ended_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    #: 失败原因 / 备注（成功时留空）。
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)


class MaterialFigure(Base):
    """材料里的图（原先只存在于 `maps/<材料>/figures.yaml`）。

    图在这个流水线里是**一等公民**：正文用"The following image shows…"把图当作解释主体，
    所以图要单独过视觉理解，读出来的空间关系（谁连谁、箭头朝哪）常常是正文没写全的。
    调度要问"这个材料还有哪些图没读过"，这必须问库。
    """

    __tablename__ = "material_figures"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    material_id: Mapped[int] = mapped_column(ForeignKey("materials.id"), nullable=False)
    figure_id: Mapped[str] = mapped_column(String(64), nullable=False)
    src: Mapped[str] = mapped_column(Text, default="", nullable=False)
    slice_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    start_line: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    end_line: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    caption: Mapped[str] = mapped_column(Text, default="", nullable=False)
    kind: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    # 图关联的知识点 key（`figures.yaml` 里的 keys）与处理方式（plan）
    keys: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    plan: Mapped[str] = mapped_column(Text, default="", nullable=False)

    __table_args__ = (UniqueConstraint("material_id", "figure_id", name="uq_figure_material"),)


class TopicGroup(Base):
    """考纲的一级分组（`groups:`）。

    原先只存在于 `meta/topics.yaml` 里，库里只存了节点的 `group_key` ——
    于是"分组叫什么、排第几"这件事没进库，界面上的分组名只能靠读文件补
    （`bank_import._decorate_group_names` 就是干这个的）。树要长大的话，
    这份定义必须和树在一起。
    """

    __tablename__ = "topic_groups"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    order_index: Mapped[int] = mapped_column(Integer, default=999, nullable=False)


class MetaDocument(Base):
    """少数**单文件配置**的正文（目前是考纲 `topics.yaml`）。

    题库与知识空间进了库之后，剩下的就是这类文件：它们不是"一题一个"，但同样不该
    成为权威 —— 否则"库里改了、文件没改"这种漂移迟早发生。放这里之后，
    仓库里可以不留它们；需要时由 `pipeline.bankfile materialize` 现场生成。
    """

    __tablename__ = "meta_documents"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AiUsage(Base):
    """按天累计的 AI 用量，用于配额与成本观察。

    刻意用**每日聚合**而不是逐次日志：逐次日志会无界增长，而这里真正要回答的
    问题只有「今天调了多少次、花了多少 token」。需要排查单次失败时，
    保留最近一次的错误摘要就够了（`last_error`）。
    """

    __tablename__ = "ai_usage"

    date: Mapped[date_type] = mapped_column(Date, primary_key=True)
    calls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 最近一次调用的耗时与错误摘要（错误串截断，避免把大段响应写进库）
    last_latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


# ---------------------------------------------------------------- 对话（学习前台）


class Conversation(Base):
    """一次对话 —— 学习前台的容器（原先只存在于浏览器内存里）。

    为什么它得是一等公民：

    * **它是用户最贵的产物**。一年的学习轨迹就是一串对话，而它只在两种时候被读：
      接着聊、以及被扫（从轨迹里提取薄弱点）。两者都要求一个稳定的地址。
    * **它要能被引用**。题目卡片、材料引用、掌握度变动都长在消息上，
      前端内存里的一个数组地址不了任何东西。

    """

    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    #: 这个标题还是**系统起的**（首句的前 60 字），可以再让模型起一个好点的。
    #:
    #: 第一轮答完之后，模型会给这条会话起名并把它置 False（见 `routers/chat.py`
    #: 的 `_auto_title`）；用户手动改过名也置 False —— 人起了名字就不再动它。
    #: 建会话时就写了标题的（`POST /chat/conversations` 带 title）同样是 False。
    # `server_default` 不是装饰：**新库**（`create_all`，比如手机首次安装）拿到的
    # DDL 只有它才会带 DEFAULT；同步的裸 INSERT 不带这一列时靠它兜底
    #（见 `sync._apply_row` 里那段说明）。
    title_auto: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="1", nullable=False
    )
    #: 置顶：用户自己钉在列表最上面的那些。
    #:
    #: **它是"收藏夹"，不是"搬走"**：置顶的会话在置顶区有一份**副本**，
    #: 原位置（归档 / 未归档）里那份**不动** —— 用户的原话："对话置顶后位置不变，
    #: 在置顶处加副本"。
    pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: 归档：收起不看的那些（就是"文件夹"那层逻辑，只有一层，不分级）。
    #: 新建的对话默认 False = 落在**未归档**里；置顶与它互不干涉
    #:（用户："有没有归档都可以置顶"），所以归档**不会**顺手取消置顶。
    archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), nullable=False
    )
    # 刻意**没有** `onupdate`：这一列是"最近活动"，而列表按它排序 ——
    # 有 onupdate 的话，改个标题或取消置顶都会把这条会话顶到最前面去
    # （实测撞到：取消置顶之后它反而排得更前）。谁真正动过它，由"产生了消息"
    # 来定义，所以只有 `post_message` 显式写它。
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), nullable=False
    )

    #: 所属分组 —— 一个**路径**（`''` = 根，`考研/数学` = 两层）。
    #:
    #: 为什么是路径而不是 `parent_id`：树是"给人看的一条线"，而路径让
    #: "整个子树搬走 / 改名"变成一次前缀替换；`parent_id` 要递归改一串行，
    #: 还得防环。分组不深（是人自己分的），路径的代价只是字符串长一点。
    folder: Mapped[str] = mapped_column(String(240), default="", nullable=False)

    __table_args__ = (Index("ix_conversations_updated", "updated_at"),)


class ConversationFolder(Base):
    """对话分组 —— 一条**目录**（会话的归属写在 `Conversation.folder` 上）。

    为什么除了 `folder` 列还要一张表：**空分组也得存在**。
    只从会话反推目录的话，新建一个分组、还没往里放东西时它会立刻消失 ——
    用户点完「新建分组」界面上什么都没发生，只会以为坏了。

    它同时是"这个分组存在"的凭据，和"这个分组有几级"的来源。
    """

    __tablename__ = "conversation_folders"

    id: Mapped[int] = mapped_column(BigAutoId, primary_key=True, autoincrement=True)
    #: 跨机器的稳定身份（见 `app/sync.py`）。自增 `id` 只在**这台机器**上有意义，
    #: 两台机器各自新建分组都会得到 1、2、3……，拿它当同步键会把两条不同的分组
    #: 合并成一条。同步只认 `uid`，`id` 不进远端。
    uid: Mapped[str] = mapped_column(
        String(32), default=lambda: uuid.uuid4().hex, nullable=False, index=True
    )
    path: Mapped[str] = mapped_column(String(240), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, server_default=func.now(), nullable=False)

    __table_args__ = (Index("ix_conv_folders_path", "path", unique=True),)


class Message(Base):
    """对话里的一条消息。

    ## 为什么是树而不是列表

    `parent_id` 让「再生成 / 换个说法重问」是**新增一个分支**，而不是覆盖原文 ——
    覆盖会毁掉轨迹，而轨迹恰恰是这里最值钱的东西（"我当时是怎么被讲明白的"）。
    线性界面只是这棵树的一条路径：第一版可以只画一条线，但结构不锁死。

    ## 为什么流式中间态要落库

    一次生成可能十几秒。中途关页面、断网、点停止，如果只在结束时才写库，
    这十几秒的产出就没了 —— 用户看到的是白屏，而且会怀疑是自己点错了。
    所以先建一条 `status='streaming'` 的行，边收边写，结束时改成
    `ok` / `partial`（被中断，有部分产出）/ `error`。

    ## 正文为什么是零件而不是一段字

    一条回答里会长出不止一种东西：正文、模型的推理、它调用了哪个工具、
    引用材料的哪几行、就地推给你的那道题。全塞进一段字符串的话，
    前端只能靠正则猜（且永远猜不全），而"引用可点回原文"这类要求就无从谈起。
    所以 `parts` 是真相、`content` 是投影（见 `app/parts.py`）。
    """

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(BigAutoId, primary_key=True, autoincrement=True)
    #: 跨机器的稳定身份（见 `app/sync.py`）。
    #:
    #: `id` 是自增整数，只在**这台机器**上有意义 —— 两台机器各自离线写对话，
    #: 都会从同一个号段往下发；拿 `id` 当同步键，第二台机器的新消息会顶掉
    #: 第一台机器的同号消息。所以同步只认 `uid`，`id` 不进远端，
    #: 消息之间的父子引用也按 `uid` 翻译（`parent_id` ↔ `parent_uid`）。
    uid: Mapped[str] = mapped_column(
        String(32), default=lambda: uuid.uuid4().hex, nullable=False, index=True
    )
    #: **这一轮用的是哪套模式** —— 发送那一刻挂载了哪几组工具，存成 JSON 列表。
    #:
    #: 为什么要落库：模式是用户随时在改的（那排开关），改完之后"当时是怎么问的"
    #: 就再也推不出来了 —— 而它恰恰是回看对话时最要紧的一半（同一句话，
    #: 挂了资料库和只带极简模式，答案的口径完全不同）。对话树据此画出过程中的变化。
    #: 空字符串 = 这条消息早于这个字段（老消息），前端据此不画标记。
    #: `server_default` 同理（见 `Conversation.title_auto`）：新库的裸 INSERT 要它兜底。
    mounts: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    parent_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("messages.id", ondelete="CASCADE"), nullable=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # user / assistant
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # ok=完整 / streaming=正在生成 / partial=被打断但有产出 / error=失败
    status: Mapped[str] = mapped_column(String(16), default="ok", nullable=False)
    error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    finish_reason: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    model: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 消息的**真相**：零件数组（正文 / 推理 / 工具调用 / 出处 / 题卡），见 app/parts.py。
    # `content` 是它的正文投影（供列表预览与搜索），方向不能反过来 ——
    # 与「题目是事实、索引是投影」同一条规矩。
    parts: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_messages_conversation_created", "conversation_id", "created_at"),)


class Attachment(Base):
    """对话里的一个附件（用户传进来的文件）。

    ## 为什么单独一张表，而不是塞进消息的零件里

    正文得**分开放**：图片是二进制（进不了 JSON 零件），PDF 抽出来的文本可能
    几百 KB（塞进零件等于让每次翻会话都拖着它走）。所以零件里只放元数据
    （哪个附件、叫什么、多大、抽出多少字），内容按需取。

    `text` 是抽出来给**模型**看的（纯文本直接读、PDF 走 `pdftotext`）；
    抽不出来（图片就是这样）就留空 —— 那不代表附件没用，只是模型读不到内容。

    `path` 存**相对 `api/` 的路径**，绝对路径不进库：材料那一课已经教过一次
    （换台机器路径就不通），附件更不能犯。
    """

    __tablename__ = "attachments"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # 落在那条消息上。可空：先上传、再发送，中间那段时间它还没归属
    message_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("messages.id", ondelete="CASCADE"), index=True, nullable=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    mime: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    size: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    path: Mapped[str] = mapped_column(String(400), default="", nullable=False)
    #: text（读得出正文）/ pdf / image / other
    kind: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), nullable=False
    )


class LibraryJob(Base):
    """资料流水线里的一条任务：`queued → running → done`（可暂停 / 取消 / 重试）。

    为什么进库而不是放内存：拖一批文件进来，页面关掉再打开还得看得见队列；
    暂停、继续、取消都要跨请求活着。执行器是 `app/jobs.py` 那条单线程 ——
    解析与向量化都是 CPU 活，并发只会互相抢。

    进度只在**阶段边界**写（`parse` → `slice` → `embed` → `done`），不是每写一片
    就更新一次：一条切片写到一半被打断就是半个材料。
    """

    __tablename__ = "library_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: 相对资料根的路径（页面就显示这个，拖进来时与上传接口的 `rel` 同一套）
    rel: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: 绝对路径：`ingest.ingest_file` 直接吃它
    path: Mapped[str] = mapped_column(Text, default="", nullable=False, index=True)
    #: queued / running / canceled / done / failed（`paused` 是全局开关，不落在这列）
    status: Mapped[str] = mapped_column(String(16), default="queued", nullable=False, index=True)
    #: 空串 = 还没开始；parse / slice / embed / done
    stage: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    #: 给人看的一句话（阶段名、完成摘要、失败原因截断 400 字）
    message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: 跑完写进来的材料 slug；失败或取消时为空
    material_slug: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


# ============================================================================ 计划与日程
#
# **计划与日程是两件事，这是这一块唯一的硬约束。**
#
# * 计划（`plans` + `plan_items`）是"要做什么"：一份计划下挂若干条目，条目可以
#   没有日期（"把 tt-metal 那章读完"），有了日期也只是**日历日**（`due_date`）——
#   它表示"这天该做"，不表示"这天下午三点到四点做"。
# * 日程（`schedule_events`）是"哪个时段做什么"：有起止时刻，落在时间轴上。
#   它可以由某个条目排期而来（`item_id`），也可以是凭空排的一件事（`item_id` 为空）。
#
# 所以：**条目带 due_date 不等于它进了日程**，反过来**日程也不要求先有条目**。
# 合并成一张表最省事，但那样就再也表达不了"这个计划我还没排期" ——
# 而这正是用户立计划时最常见的状态。
#
# 主键用 `uuid4().hex` 而不是自增整数：自增 id 在两台机器上必然撞号，
# 而 `sync._build_specs()` 只给 `messages` / `conversation_folders` 开了
# "用 uid 当同步身份"的特例；文本主键天然可跨机，不必再改那段。
def _plan_uid() -> str:
    return uuid.uuid4().hex


class Plan(Base):
    """一份计划：AI 在对话里订的，或者自己在日程页建的。

    `proposal_id` 是**凭条幂等键**：对话里那张卡片被点两次、断网重试、
    或者前后端各存了一份，都只会落到同一份计划上（唯一索引兜底）。
    """

    __tablename__ = "plans"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_plan_uid)
    proposal_id: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    title: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: 这份计划的**计划书**（Markdown）。建计划时从标题 / 目标 / 条目播下第一版，
    #: 之后由 AI 用 `read_plan_doc` / `update_plan_doc` 当工作文档维护 ——
    #: 像 coding agent 的 plan 文件。条目勾选以 `plan_items` 为准，文档不反向驱动它。
    doc: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: active / archived（归档只是收起来，不删条目）
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False, index=True)
    #: chat（AI 订的，用户确认过）/ manual（自己建的）—— 只影响界面上的来源标记
    source: Mapped[str] = mapped_column(String(16), default="manual", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class PlanItem(Base):
    """计划里的一条待办。

    `due_date` 是 **Date 不是 DateTime**：它表示日历上的一天。
    存成时刻就必须先决定"几点"，而那个几点会随机器时区漂 ——
    "周三做完"跨时区不该变成"周二晚上"。
    """

    __tablename__ = "plan_items"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_plan_uid)
    plan_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("plans.id"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: 哪一天该做（可空 = 还没排日子）
    due_date: Mapped[date_type | None] = mapped_column(Date, nullable=True, index=True)
    important: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: 预计要花多少分钟（0 = 没估）。排期时用它当默认时长。
    estimate_min: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # ---- 自动排程要用的元数据（2026-11-08）----
    #: 专注度需求：light / medium / deep。`deep` 只能落在高精力时段（见 wellbeing）
    focus: Mapped[str] = mapped_column(String(12), default="medium", nullable=False)
    #: 学科归类：art / science / skill / other。文理交替与门禁默认映射都看它
    domain: Mapped[str] = mapped_column(String(12), default="other", nullable=False)
    #: 能不能拆成几块。不能拆的（"听完这一讲"）整块排，装不下就不排
    splittable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: 单块上限（0 = 用求解器默认的 90 分钟）
    max_chunk_min: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: 最早哪天可以开始（和 `due_date` 一起夹出可用窗口）
    earliest_date: Mapped[date_type | None] = mapped_column(Date, nullable=True)
    #: 质量门禁策略 key（空 = 按 domain 的默认映射，见 `app/gates.py`）
    gate: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    #: 关联的知识点 key（测验门禁按它抽题；空列表 = 不指定）
    points: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    done: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    done_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    sort: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ScheduleEvent(Base):
    """日程上的一个时间块。

    三处来源，**互不依赖**：

    * `item_id` 非空 —— 从某条待办排过来的（标题仍存在本行，排期时可以改叫别的）；
    * `sub_id` 非空 —— 从一份日历订阅（iCalendar）同步来的；
    * 两者都为空 —— 在日历上直接新建的。

    订阅来的块带 `uid` / `occurrence`：同一条 ICS 事件会展开成很多块（每周一次），
    `(uid, occurrence)` 是其中一块的**稳定身份**，重新同步时靠它认"还是那一块"，
    而不是按标题时间瞎猜（课表里同名同时间的两节课是有的）。
    """

    __tablename__ = "schedule_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_plan_uid)
    #: 凭条幂等键（与 `plans.proposal_id` 同一个用途）：同一张排期卡点两次
    #: 不该在日历上排出两块一样的。手动新建的事件这里为空。
    proposal_id: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    item_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("plan_items.id"), nullable=True, index=True
    )
    #: 来自哪份订阅（见下面 `CalendarSubscription`）。为空 = 不是订阅读进来的。
    sub_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    #: ICS 的 UID + 这一次发生的原始开始时刻（UTC，`20261109T060000Z`）。
    uid: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    occurrence: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    title: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: 地点（ICS 的 LOCATION）：课表的教室就写在这里，悬停与日视图那栏会读它
    location: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    start_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, index=True)
    end_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    #: 全天事件：起止落在同一日历日的 00:00–24:00，渲染时不占时间轴的高度
    all_day: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: 用户动过这一块（拖过 / 改过标题）。重新同步时**跳过**它：
    #: 订阅是"别处的事实"，但用户把一节课挪到晚上这件事也是事实，不该被抹掉。
    detached: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: 用户钉住了这一块：求解器不许挪它（与"订阅块不动"是两套理由，一起生效）
    locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: 哪一次重排产生的（`schedule_runs.id`）。重排只回收**自己排的**，不动别人
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class CalendarSubscription(Base):
    """一份日历订阅：一个 iCalendar 地址（http/https），同步成日历上的块。

    用户的原话是"我们的日历需要支持标准协议"，所以这里不认识"课表"：
    地址给什么（课表、会议、节假日）就同步什么，解析见 `app/ics.py`。

    `url` 里常常带着**能力令牌**（学校课表的地址就是一串 JWT，拿到地址就能读到
    整个人的课表）。因此这张表在 `datapack.BOUNDARY` 里归 `derived`：
    不上云、不进用户包 —— 换台机器重新订阅一次即可，而同步下来的时间块
    （`schedule_events`）本身是用户数据，照常同步。
    """

    __tablename__ = "calendar_subscriptions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_plan_uid)
    url: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: 显示名：优先取 ICS 的 `X-WR-CALNAME`，没有就用主机名
    title: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_sync_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    #: 给人看的一句话：`已同步 342 块` / 失败原因（截断）
    last_result: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class SleepLog(Base):
    """一条睡眠记录：只记**两端**，时长是算出来的。

    为什么不让用户填"昨晚睡了 7 小时"：那样就丢掉了"几点睡"。而作息漂移
    （凌晨三点睡够八小时）与规律作息的差别，正是健康模块要看的 —— 睡眠债
    算的是"该睡的时候没睡"，不是"总时长够不够"。

    打卡是"开一条 / 合上一条"：

    * 「睡了」→ 建一行，`wake_at` 空着（`bed_at` 是那一刻）；
    * 「起床了」→ 把最近那条没合上的补上 `wake_at`。

    所以任何时候最多只有一条开口的记录。两端都可空：先按了"起床"、没按"睡了"
    的那天也要能记下来（不能因为缺一端就丢掉"我 7 点起了"这件事）。
    """

    __tablename__ = "sleep_logs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_plan_uid)
    bed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    wake_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    #: 主观质量 0 未填 / 1..5
    quality: Mapped[int] = mapped_column(SmallInteger, default=0, nullable=False)
    #: manual（打卡）/ inferred（以后从活跃时间推断，落库时标明来历）
    source: Mapped[str] = mapped_column(String(16), default="manual", nullable=False)
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class HealthRegime(Base):
    """身体状态档位的**时间线**：某段日期里按哪套精力策略算。

    "不同时期我的身体机能在变"（用户原话）不是靠改代码表达，而是靠这张表：
    比如 `2026-11-06 ~ 2026-11-20 / recovery`（病后恢复），其余日子走设置里的
    默认档位。`profile` 指向 `app/wellbeing` 注册表里的策略 key，`params` 覆盖
    那套策略的默认参数（不同的恢复期可以松紧不同）。

    查询规则：覆盖该日的档位里**开始日最晚**的那条赢；没有就用默认。
    """

    __tablename__ = "health_regimes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_plan_uid)
    title: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    start_date: Mapped[date_type] = mapped_column(Date, nullable=False, index=True)
    #: 空 = 一直有效（不设结束日）
    end_date: Mapped[date_type | None] = mapped_column(Date, nullable=True)
    profile: Mapped[str] = mapped_column(String(40), default="standard", nullable=False)
    #: 覆盖该 profile 的默认参数（只存差异，不存整份）
    params: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ScheduleRun(Base):
    """一次重排的**快照**：撤销靠它，不靠全库回滚。

    重排要做三件事：删掉上一轮自己排的块、写下新的块、有时还要挪动已有的块。
    这些都发生在同一张 `schedule_events` 上，而且用户可能在这之间手动改过别的东西 ——
    整库回滚会连手动改动一起抹掉。所以只记这一次**动过的行**：

    * `before` —— 被删/被挪之前的整行原值（JSON），撤销时按原 id 写回去；
    * `created` —— 这次新建的行 id，撤销时按 id 删掉。

    再加 `profile` / `regime_id`，事后能回答"那天为什么只排了两小时"：
    求解器当时看到的是哪套精力策略、哪一档身体状态。
    """

    __tablename__ = "schedule_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_plan_uid)
    #: replan（重排）/ manual（以后"手动应用一次提案"也记一条）
    kind: Mapped[str] = mapped_column(String(16), default="replan", nullable=False)
    #: 被删 / 被挪之前的整行（字段名与 `schedule_events` 一致）
    before: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    #: 这次新建的行 id
    created: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    #: 求解时的精力档位与生效的身体状态档位（空 = 用默认）
    profile: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    regime_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: 撤销过就置上时刻；一条 run 只能撤一次（再撤是 409，不是静默成功）
    undone_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Memory(Base):
    """一条**全局记忆**：跨对话长期带着的、关于这个人的信息。

    与「对话历史」和「笔记」的分界：

    * 对话历史是**这一条线**的上下文，换一条对话就看不见了；
    * 笔记是他自己写的、按主题归档的正文；
    * 记忆是 AI 主动记下的**关于他**的长期事实（偏好、背景、在做的事），
      每条都短，跨所有对话都成立；它**不自动进提示词**，由 AI 需要时用
      `list_memory` 查（见 `app/memory.py`）。

    所以它不是"聊天记录摘要"，也不是"知识"—— 记错一条会一直影响之后的每轮对话，
    于是界面必须让人能看见、能改、能删（设置 → 记忆）。
    """

    __tablename__ = "memories"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_plan_uid)
    title: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    body: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: profile / preference / fact / project（见 `app/memory.py` 的 KINDS）
    kind: Mapped[str] = mapped_column(String(16), default="fact", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class GateRun(Base):
    """一次质量门禁的作答记录。

    **没过也要落库**：不通过的记录是"下一次重排该优先补偿它"的唯一依据
    （`scheduler.build_input` 会把最近一次没过的条目加进 `priority_boost`）。
    只记通过的话，没过的块就静默地留在原地，第二天照旧排在最后。

    `payload` 存判分依据（对了哪几题、错在哪、是不是走了自评降级）——
    门禁的价值在于"能回看当时凭什么判它过了"。
    """

    __tablename__ = "gate_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_plan_uid)
    #: 门禁是给**条目**上的（块是条目的一次排期，条目才是"这件事做完了没"）
    item_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    event_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    gate: Mapped[str] = mapped_column(String(32), default="none", nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class LensRecord(Base):
    """一条「滤镜挂在哪儿」的记录。

    为什么是表而不是设置（2026-11-09 用户："lens 属于数据，由用户手动导入，
    用之前的云+缓存方案持久化"）：滤镜是用户自己的痕迹 —— 挂在某条消息上、
    沿对话树继承。它该跟着对话走（Turso 行级同步、用户数据包），而不是混进
    `config/settings.local.json`（那份是给人复制给新用户的**设置**）。

    `message_uid` 存**消息的跨机身份**（`Message.uid`；空串 = 整条对话）：
    `id` 只在单机有意义，两台机器各自从同一号段发号会撞（与 messages 的
    同步理由同一个，见 `Message.uid` 那段）。
    """

    __tablename__ = "lens_records"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_plan_uid)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    #: 空串 = 挂在**整条对话**上（第一条消息还没有 id 时也能装）
    message_uid: Mapped[str] = mapped_column(
        String(32), default="", nullable=False, index=True
    )
    #: 滤镜的 key（`<数据目录>/lens/<key>.md` 的裸名）
    lens_key: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 装上 = True / 取下 = False。**取下也留记录**：它要盖住更靠根的那条"装上"
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: 操作时间（同一 key 取最近一次；与设置里那条记录的 `at` 同义）
    at: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class LensDef(Base):
    """一条滤镜的**定义本体**（提示词）。

    2026-11-11 用户：「把我的 lens 导入一下数据库，它属于我的用户数据」。
    在那之前它只是 `<数据目录>/lens/*.md` 一堆文件 —— 不在同步里、不进用户包，
    换台机器就没了（与 `lens_records` 那种"挂载记录"是两个东西）。

    文件仍然保留，但降级成**镜像**：`lens.sync_folder()` 在启动时把两边对齐，
    界面里的增删改写库的同时也写文件（`ls` 得到、备份得到、手改也还认）。

    `digest` 是那份 md 原文的 sha256，用来判断"文件被手改过没有"：
    两边不一致时文件优先（人直接编辑文件是最强的意图表达），再同步回库。
    """

    __tablename__ = "lenses"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    label: Mapped[str] = mapped_column(String(64), nullable=False)
    #: flow / tone / discipline（合法值见 `lens.KINDS`）
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    hint: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    prompt: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: md 原文的 sha256；空串 = 还没和文件对齐过
    digest: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    #: 删掉的留墓碑：否则"远端删了、本地文件还在"会在下次启动时被导回来
    deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Note(Base):
    """一份笔记的**云副本**：vault 里那份 Markdown 在库里的镜像。

    为什么要有它：桌面端直连本地 vault（文件信箱），可**手机上没有 Obsidian**，
    没有文件可读、也没有插件可发请求。于是把笔记做成一张普通的用户表，顺着
    现有的 Turso 同步通道走 —— 手机端 LLM 读写这张表，桌面端由插件双向对齐。
    桌面端自己**永远不写这里**（见 `app/vault.py` 的路由）：文件才是那边的权威，
    两边都写就会互相覆盖。

    `path` 是 vault 内的相对路径，与插件口径一致；`body` 是**去掉 frontmatter
    的正文**，`hash` 与插件 `sha16(body)` 完全同算法 —— 两边靠它判"是不是同一版"，
    算法不能各写一套（见 `notecloud.py` 的 `sha16`）。

    同步时整行加密（`PRIVATE_TABLES`）：笔记是关于这个人的内容，与对话同等对待。
    """

    __tablename__ = "notes"

    path: Mapped[str] = mapped_column(String(512), primary_key=True)
    body: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: sha256(body) 的前 16 位十六进制，与 `obsidian-bridge/main.js` 的 sha16 同一算法
    hash: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    #: 文件 mtime（epoch 秒）。排序与"谁更新"的展示用，**不参与**冲突判定
    mtime: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    #: 正文字节数（UTF-8）
    size: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )
