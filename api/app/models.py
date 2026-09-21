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


class User(Base):
    """本机用户 —— 单用户本地形态下**只会有一行**。

    账号面（注册 / 登录 / 会话 / CSRF）已整体删除（见 `app/deps.py`）。
    `email` 与 `password_hash` 是历史遗留的列：留着它们，是为了将来真要做
    "多设备同步"时不必再动一次数据模型 —— 那时它们会重新有用。
    """

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, server_default=func.now(), nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    disabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<User {self.email}>"

    # `sessions` 表与它的模型随账号面一起删掉了（服务端会话没有存在意义了：
    # 本地没有"登录"这个动作可失效，也没有第二个人可以冒充）。


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


class UserQuestion(Base):
    """用户题单里的一道题 —— **自己出的**，与公共题库分开。

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

    __tablename__ = "user_questions"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
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

    __table_args__ = (Index("ix_user_questions_user_created", "user_id", "created_at"),)


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
    """用户对一道题的累计学习状态。

    ## 字段被刻意劈成两半，这是跨设备正确性的地基

    **计数**（`attempts / correct / partial / wrong / first_at / last_at /
    last_status / last_score / last_response`）只由 `attempts` 流水增量维护，
    **永远不接受客户端上传**。原因：客户端上传的是「我这台设备看到的累计值」，
    多设备下按 last-write-wins 合并会让后到的那份**静默覆盖**另一台设备的作答
    （计数不增、用户毫无察觉）。流水是增量、插入是精确一次的，相加天然正确。

    **补丁**（`patch` 里的 sm2 / note / streak）与 **动作**
    （`mastered` / `flagged`）是用户的主观点击，没有可加和的语义，
    照旧按 `client_rev` 做 last-write-wins。

    这条边界要在服务端做成硬约束：升级用的 `patches` 里若混入计数字段，
    一律忽略（见 `sync_ops.apply_patches`）。
    """

    __tablename__ = "records"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
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

    __table_args__ = (Index("ix_records_user_updated", "user_id", "updated_at"),)


class Attempt(Base):
    """逐次作答的流水 —— 计数的**唯一来源**。

    1. `id` 由客户端生成，主键约束让网络重试天然幂等（`ON CONFLICT DO NOTHING`）；
    2. 服务端只对**真正插入成功**的流水累加计数与每日统计，
       所以「请求发出但响应丢了、客户端重发」不会重复计数 ——
       这是跨设备场景下最关键的一条保证；
    3. 逐次流水保留下来，日后聚合口径若改动，可以重算历史而无需用户重做题目。
    """

    __tablename__ = "attempts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
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

    __table_args__ = (
        UniqueConstraint("user_id", "id", name="uq_attempts_user_id_id"),
        Index("ix_attempts_user_at", "user_id", "at"),
    )


class DayStat(Base):
    """每日聚合，供热力图与趋势使用。

    topics 里存「一级主题 → 次数」，热力图按主题筛选就靠它。
    """

    __tablename__ = "days"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    date: Mapped[date_type] = mapped_column(Date, primary_key=True)
    answers: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    correct: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    topics: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)


class UserSettings(Base):
    """界面设置 + 选题篮 + 置顶主题，整体存一个 JSON。

    这些字段的演进速度远快于学习数据，逐字段建列只会让每次加设置都要写迁移。
    """

    __tablename__ = "user_settings"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
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

    __table_args__ = (UniqueConstraint("material_id", "slice_id", name="uq_slice_material"),)


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
    问题只有「今天这个用户调了多少次、花了多少 token」。需要排查单次失败时，
    保留最近一次的错误摘要就够了（`last_error`）。
    """

    __tablename__ = "ai_usage"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
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

    归属从出生就带上（`user_id`）：这是多租户那条缝的落点 ——
    以后加团队 / 题库归属时，旧数据不需要回填。
    """

    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    title: Mapped[str] = mapped_column(String(120), default="", nullable=False)
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

    __table_args__ = (Index("ix_conversations_user_updated", "user_id", "updated_at"),)


class ConversationFolder(Base):
    """对话分组 —— 一条**目录**（会话的归属写在 `Conversation.folder` 上）。

    为什么除了 `folder` 列还要一张表：**空分组也得存在**。
    只从会话反推目录的话，新建一个分组、还没往里放东西时它会立刻消失 ——
    用户点完「新建分组」界面上什么都没发生，只会以为坏了。

    它同时是"这个分组存在"的凭据，和"这个分组有几级"的来源。
    """

    __tablename__ = "conversation_folders"

    id: Mapped[int] = mapped_column(BigAutoId, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    path: Mapped[str] = mapped_column(String(240), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, server_default=func.now(), nullable=False)

    __table_args__ = (Index("ix_conv_folders_user_path", "user_id", "path", unique=True),)


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
    #: **这一轮用的是哪套模式** —— 发送那一刻挂载了哪几组工具，存成 JSON 列表。
    #:
    #: 为什么要落库：模式是用户随时在改的（那排开关），改完之后"当时是怎么问的"
    #: 就再也推不出来了 —— 而它恰恰是回看对话时最要紧的一半（同一句话，
    #: 挂了资料库和只带极简模式，答案的口径完全不同）。对话树据此画出过程中的变化。
    #: 空字符串 = 这条消息早于这个字段（老消息），前端据此不画标记。
    mounts: Mapped[str] = mapped_column(Text, default="", nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    # 冗余一份 user_id：每个查询强制带它（与 records / attempts 同一条规矩），
    # 免得"查消息"这条最热的路径每次都要先 join 会话
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
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

    __table_args__ = (
        Index("ix_messages_conversation_created", "conversation_id", "created_at"),
        Index("ix_messages_user_created", "user_id", "created_at"),
    )


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
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
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
