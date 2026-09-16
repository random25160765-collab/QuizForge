"""数据模型。

## 题目为什么用「提升列 + payload JSONB」

题的权威表示就是 `tools/question_parser.py` 产出的那个 dict，
前端（以及离线构建产物）消费的也是同一份 JSON。若把它拆成
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
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

# Postgres 上用 JSONB（可索引、可查询）；其它方言退化为普通 JSON，
# 代价为零，好处是测试可以直接跑在 SQLite 上。
JSONType = JSON().with_variant(JSONB(), "postgresql")


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # 统一小写存储；登录时也做小写归一化，避免大小写不同的重复账号
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    sessions: Mapped[list["Session"]] = relationship(back_populates="user", cascade="all, delete-orphan")

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<User {self.email}>"


class Session(Base):
    """服务端会话。

    选服务端会话而不是 JWT：数据隔离要求每个查询强制带 user_id，
    而服务端会话可以一键失效（改密码、封禁、登出全部设备），
    前端也不需要任何 token 存储与续期逻辑。
    表里只存令牌的哈希，即使库被读走也无法直接冒充登录。
    """

    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    user_agent: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    ip: Mapped[str] = mapped_column(String(64), default="", nullable=False)

    user: Mapped[User] = relationship(back_populates="sessions")


class BankVersion(Base):
    """一次题库导入的快照记录，用于 ETag 协商缓存与回滚定位。"""

    __tablename__ = "bank_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    content_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
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
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
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
    # 但"原文"必须留着 —— 导出、离线构建、契约改动后重新解析，全靠它。
    # 没有这一列，库就只能算半个权威（丢不掉文件）。
    raw_markdown: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    # 与 question_to_dict() 完全一致的权威 JSON
    payload: Mapped[dict] = mapped_column(JSONType, nullable=False)
    # 判重与「变了没有」都看它（正文哈希，与 id 无关）
    content_hash: Mapped[str] = mapped_column(String(64), index=True, nullable=False)

    bank_version_id: Mapped[int | None] = mapped_column(ForeignKey("bank_versions.id", ondelete="SET NULL"))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_questions_topic_type", "topic", "type"),
        Index("ix_questions_status_layer", "status", "layer"),
    )


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
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
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
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # 归属哪一天由**客户端的本地日期**决定，随流水一起上报。
    # 不用服务端时区从 at 反推：服务器跑 UTC 时，用户凌晨作答会被算到前一天，
    # 热力图与「今日已练」就会对不上。
    day: Mapped[date_type] = mapped_column(Date, index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    response: Mapped[dict | list | None] = mapped_column(JSONType)
    # 作答时的主题快照：即使主题树后续调整，历史热力图仍能按当时的归属统计
    topic_key: Mapped[str] = mapped_column(String(64), default="", index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

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
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
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
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class KnowledgePoint(Base):
    """知识空间：一个可考知识点（claim 簇）。

    **出题的原料单位是它，不是切片**：一个点常常横跨好几段原文，
    所以出处是一对多（`point_sources`），而不是一个行区间字段。
    """

    __tablename__ = "knowledge_points"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    material_id: Mapped[int] = mapped_column(
        ForeignKey("materials.id", ondelete="CASCADE"), index=True, nullable=False
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
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
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
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
