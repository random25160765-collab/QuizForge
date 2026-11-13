"""云端-缓存架构：Turso 是云上的那份数据，本地 SQLite 是工作副本。

## 两个角色（2026-11-09 明确）

* **云端（Turso）**：所有设备共享的那一份。它不参与页面渲染，但它是
  "换台机器为什么能看到同样的数据"的答案 —— 用户数据在云上的权威副本。
* **缓存 / 工作副本（本地 SQLite）**：应用直接读写的那份。断网照常用，
  所有页面、检索、流水线都打它；同步只是把变化在两边对齐。

于是"本地删掉的对话在另一台也消失"不是 bug，而是缓存与云端对齐的
正常结果（删除以 tombstone 行上传，对端拉到时删本地行）。

## 同步什么（全部用户数据，自动进行）

* **行级表**：`SYNC_TABLES` = 所有 `user` 表减去 `days`（纯派生）。对话、
  分组、消息、附件、自出的题、答题流水、**全局记忆**、计划（含计划书正文）、
  条目、日程、睡眠、健康档位、门禁记录、重排快照、设置。每张表按
  `(表, 主键)` 增量双向同步。
* **资料整包**：`materials` / `material_slices`（含正文）/ `material_figures`
  三张表主键会撞号，走 `library_cloud.py` 的整包对齐，不在这里逐行传。
* **自动执行**：`app/autosync.py` 是"有变化才动" —— 本地一改就推，
  每 5 秒问一次远端有没有新行，60 秒兜底一轮整包。手动按钮、`make sync`、
  CLI 走同一个 `run_full()` 入口。

**不上云的**：纯派生数据（`days`、向量、向量化跑单、流水线队列）—— 能重算；
`calendar_subscriptions` —— 地址里常带能力令牌，属于凭据；题库 —— 出题机的
产物，走题库包（`make bank-export` / `bank-import`），不随用户日常使用变化。

## 它替换的是什么

2026-11-08 之前，换机器靠的是"整库快照 + git"（`tools/db_snapshot.py`）。
那条路的根本问题是**单向且不可合并**：两台机器各写各的，谁最后提交谁覆盖，
另一边的对话与进度就没了（`docs/STATUS.md` 里早就写着这一条）。
现在改成：本地 SQLite 是日常读写的工作副本，Turso 是云上那份共享副本，
每张表按行同步，两边都能写。

## 一行的身份与版本

* **身份**：一张表的主键。但 `messages` / `conversation_folders` 的自增
  `id` 两台机器会撞号（各自从 1 开始），所以这两张表另有一个随机 `uid`，
  同步只认它。消息的 `parent_id`、附件的 `message_id` 在传输时翻译成
  `uid`，落到对端再翻回本地 `id`。
* **版本**：一个 epoch 秒的浮点数 `at`。触发器在**每次写行时**更新它，
  取 `max(墙上时钟, 全库已知最大 at + 1e-6)` —— 后一半保证"刚拉下来的远端
  改动"不会被本机一个慢时钟的写入按回去（因果单调）。
* **合并**：last-write-wins，按 `(at)` 比。两个设备**同一毫秒**改同一行的
  概率可以忽略；真撞上就按"先到先得"，不额外引入设备序。

## 本地三张表（不是 ORM 模型）

它们不进 `Base.metadata`，也不进数据包（`datapack.BOUNDARY` 只认业务表）：

    qf_sync_journal  脏行集合：每行一条，`(tbl, pk)` 唯一
    qf_sync_rows     影子：每行最后一次被看到/写入的 at 与删除标记
    qf_sync_state    本机同步状态：origin / 游标 / 上次结果

**为什么 journal 是脏集合而不是流水账**：一条流式回答会更新
`messages` 几十上百次，流水账会把库撑大几十倍；推送只关心"这行现在是脏的"。
推送成功后按 `(tbl, pk, at)` **条件删除** —— 期间又变了（at 变了）就留着下轮再推。

触发器写在每张同步表上（`CREATE TRIGGER qf_sync1_*`）。拉取应用远端行时，
先在同一个事务里写 `qf_sync_state('paused','1')`，触发器据此静默，
避免"把刚拉下来的东西又标成自己的改动"。

## 远端三张表

    qf_sync_rows  当前镜像：`(tbl,pk)` 唯一，带 at / origin / deleted / enc / payload
    qf_sync_log   增量日志：每次推送一条，按 seq 递增；拉取按游标读它
    qf_sync_meta  盐 / 密钥校验值 / 格式版本

首次同步（或游标缺失）走**全量镜像**；之后只读日志。
`qf_sync_rows` 让新机器不必重放全部历史，日志则让日常同步只传变化。

## 隐私：端到端加密

`PRIVATE_TABLES`（对话 / 分组 / 消息 / 附件 / **记忆** / **计划** / **笔记**）的
**payload 整体加密**：AES-256-GCM，密钥由 **Turso 的地址 + 令牌**经 HMAC-SHA256
推出来（2026-11-10 起），盐存在远端 `qf_sync_meta`，并有一个 HMAC 校验值用于
尽早发现"两台机器连的不是同一个库"。
Turso 只看到 `tbl / pk / at / origin` 和密文。其余表照旧明文上传
（用户原话："我的个人数据（对话）端到端加密后上传。其他数据不加密上传。"；
2026-11-09 他又要求把**全局记忆**与**计划书正文**也加密 —— 已经划进来）。

**为什么不再让用户填口令**（用户 2026-11-10）：数据库是用户私有的，地址 + 令牌
已经等于身份；再让用户保管一份口令，只是多一个会丢、会填错、会两台不一致的东西。
代价：**令牌一换，历史密文就解不开了**（换令牌属于迁移，不是日常操作）。

表划进 `PRIVATE_TABLES` 之前已经同步过的明文行，由
`_schedule_private_rescope` 在升级后重推一遍换成密文（每台机器一次）。
密钥换代同理：`_migrate_key` 把远端 `check` 换成新密钥的，并把本机加密行整表
重推（用户："现有密文作废，全部重新加密"）。

**密钥不进 Turso，也不进 git**：它由本机设置文件里的 turso 凭据算出来，从不落盘。
没有凭据时拒绝同步（而不是偷偷明文上传），也不推进游标（否则之后补上也拉不到
错过的那段）。

## 设置里的秘密

`app_settings` 会同步，但 `ai.apiKey` / `search.apiKey` / `turso.token`
在**上传前被抹空**；拉取时保留本机那一份。`turso` / `obsidian` /
`library_roots` 这三个块整个保留本机的（连接与路径是机器属性，不是数据）。
其余设置（主题、痕迹 notes/places、选题篮……）照常双向同步。

## 已知取舍

* `records` 的计数列**不从远端覆盖**：它由流水增量维护（见 `sync_ops.py`）。
  拉取 `attempts` 时，真正新插入的那些会当场累加进 `records` / `days`，
  所以并集是对的；`days` 本身不参与同步（纯派生）。
* `apply_resets`（重新设基线）只影响本机；它没有对应的流水，无法可靠合并。
* 远端日志目前**不裁剪**。单人使用下它每天最多几十条；真长到碍事时再按
  `seq` 做保留窗口（那时要在 meta 里记 `log_floor`，否则离线太久的机器会漏段）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date as date_type, datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import Engine
from sqlalchemy.orm import Session as OrmSession

from . import models as _models  # noqa: F401 —— 先让 Base.metadata 长齐
# 加密原语在 Android 上要换实现（没有 cp314 wheel），取用点收在 crypto_compat。
from .crypto_compat import AESGCM, InvalidTag, Scrypt
from . import settings_file
from .settings_store import normalize
from .datapack import USER_TABLES
from .db import Base, get_engine, get_session_factory

log = logging.getLogger("quizforge.sync")

#: 同步哪些表。**用户表减去 `days`**：`days` 是流水的日聚合，收到流水时
#: 由 `sync_ops.bump_days` 当场重算，再传一份绝对值只会和本地增量打架。
#: 题库（bank）不在其中：它是出题机的产物，走题库包（`make bank-export`），
#: 体积大且不因用户的日常使用而改变。
SYNC_TABLES: tuple[str, ...] = tuple(t for t in USER_TABLES if t != "days")

#: 端到端加密的表。用户原话："我的个人数据（对话）端到端加密后上传。"
#: 2026-11-09 追加：**全局记忆**和**计划书正文**也是"关于他"的内容，
#: 用户明确要求加密。`plans` 是整行加密（标题 / 目标 / `doc` 正文）；
#: 条目（`plan_items`）仍是明文 —— 按表加密的粒度就这样，条目标题一般
#: 只是"读 3.1 节"这种动作。
PRIVATE_TABLES: frozenset[str] = frozenset(
    {
        "conversations",
        "conversation_folders",
        "messages",
        "attachments",
        "memories",
        "plans",
        # 笔记正文（2026-11-09）：手机端没有 Obsidian，读改都落在这张表。
        # 它装的是用户自己写的东西，与对话正文同一档 —— 只让云看见密文。
        "notes",
        # 滤镜定义（2026-11-11）：用户自己写的提示词，可能带着他的表达习惯与
        # 私事（"用我女儿的名字举例"这种）。与笔记同一档。
        "lenses",
        # 碎片（2026-11-13）：随手写的一句话。它是"关于这个人的内容"里最直白的
        # 一种 —— 想到什么就记什么，不该让云看见明文。与笔记同一档。
        "scratch",
    }
)

#: 后来才划进 `PRIVATE_TABLES` 的表（2026-11-09：全局记忆、计划书）。
#: 远端可能已经存着它们的**明文**行；每个库跑一次重推，用密文覆盖
#: （见 `_schedule_private_rescope`）。以后再有表划进来，改这里 + 抬版本号。
_RESCOPE_PRIVATE: tuple[str, ...] = ("memories", "plans")
_RESCOPE_STATE_KEY = "private_rescope_v1"

#: 密钥算法换代（2026-11-10：人手口令 → 由 turso 凭据派生）的一次性开关。
#: 远端 `qf_sync_meta` 里没有它 = 旧库，里面的密文按旧口令加密 —— 一律作废，
#: 由 `_migrate_key()` 重写校验值并把本机加密行整表重推。
_KEY_TAG = "turso-v1"
_KEY_TAG_META = "key"

#: 哪些表已经做过基线（逗号分隔）。**按表记**而不是一个 `baseline_done`：
#: 后来才加进 `SYNC_TABLES` 的表要有自己的第一次（见 `_baseline` 的说明）。
_BASELINE_STATE_KEY = "baseline_tables"

#: 拉取时的应用顺序（外键先父后子）。同一页里也按它排序。
PULL_ORDER: tuple[str, ...] = (
    "conversations",
    "conversation_folders",
    "messages",
    "attachments",
    # 滤镜定义（2026-11-11 从文件搬进库）：没有外键，先落它，挂载记录随后。
    "lenses",
    # 滤镜挂载记录（2026-11-09 从设置搬出来）：外键指对话，消息只用 uid 软引用，
    # 所以排在 messages 之后就行。
    "lens_records",
    "my_questions",
    "records",
    "attempts",
    "ai_usage",
    # 全局记忆（2026-11-09）：AI 跨对话的长期信息，换台机器要还在。
    # 它没有外键；**必须在 PULL_ORDER 里** —— 首次同步走 `_full_pull`，
    # 而 `_full_pull` 只逐表读这里列出的表，漏一张（这里曾漏过 memories）
    # 就等于那类数据永远到不了新机器，尽管推送 / 增量拉取都是通的。
    "memories",
    # 笔记云副本（2026-11-09）：同样没有外键。手机靠它读笔记，首台设备装不上
    # 就等于新机器一片空白 —— 所以必须在这里。
    "notes",
    # 碎片（2026-11-13）：没有外键。**也必须在这里** —— `_full_pull` 只逐表读
    # 这个清单，漏一张就等于那类数据永远到不了新机器（`memories` 当年漏过）。
    "scratch",
    # 外键先父后子：条目要挂计划，日程要挂条目（虽然删父行是本机显式删的，
    # 但远端按 pk 顺序回放时子行先到会插不进去）。
    "plans",
    "plan_items",
    "schedule_events",
    # 健康：睡眠记录与身体状态档位。没有外键依赖，放在日程之后只是为了读起来顺。
    "sleep_logs",
    "health_regimes",
    # 重排快照（撤销用）：它引用 `schedule_events.id`（存在 JSON 里，不是外键），
    # 所以必须排在时间块之后。
    "schedule_runs",
    # 门禁记录：挂在条目上（`item_id` 也是软引用），排在条目之后。
    "gate_runs",
    "app_settings",
)
_ORDER_INDEX = {name: i for i, name in enumerate(PULL_ORDER)}

#: 设置里这些路径不上传（本机凭据）。
_SECRET_PATHS: tuple[tuple[str, str], ...] = (
    ("ai", "apiKey"),
    ("search", "apiKey"),
)

#: 设置里这些**整块**保留本机的（连接地址 / vault 路径 / 资料根目录是机器属性）。
_MACHINE_BLOCKS: tuple[str, ...] = ("turso", "obsidian", "library_roots")

#: 上传前要抹空的设置键（含 turso 整块，所以 token 不会出去）。
#: `syncPassphrase` 只出现在**手动导出的设置 JSON**里（让新设备一次搬完），
#: 万一被前端写进了设置，也绝不能跟着同步上云 —— 那是端到端加密的钥匙。
_SCRUB_SETTINGS_BLOCKS: tuple[str, ...] = ("turso", "syncPassphrase")

_REMOTE_SCHEMA = """
CREATE TABLE IF NOT EXISTS qf_sync_rows (
  tbl      TEXT NOT NULL,
  pk       TEXT NOT NULL,
  at       REAL NOT NULL,
  origin   TEXT NOT NULL,
  deleted  INTEGER NOT NULL DEFAULT 0,
  enc      INTEGER NOT NULL DEFAULT 0,
  payload  TEXT,
  PRIMARY KEY (tbl, pk)
);
CREATE TABLE IF NOT EXISTS qf_sync_log (
  seq      INTEGER PRIMARY KEY AUTOINCREMENT,
  at       REAL NOT NULL,
  origin   TEXT NOT NULL,
  tbl      TEXT NOT NULL,
  pk       TEXT NOT NULL,
  deleted  INTEGER NOT NULL DEFAULT 0,
  enc      INTEGER NOT NULL DEFAULT 0,
  payload  TEXT
);
CREATE INDEX IF NOT EXISTS ix_qf_sync_log_seq ON qf_sync_log(seq);
CREATE TABLE IF NOT EXISTS qf_sync_meta (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL
);
"""

_LOCAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS qf_sync_journal (
  tbl TEXT NOT NULL,
  pk  TEXT NOT NULL,
  op  TEXT NOT NULL,
  at  REAL NOT NULL,
  PRIMARY KEY (tbl, pk)
);
CREATE TABLE IF NOT EXISTS qf_sync_rows (
  tbl     TEXT NOT NULL,
  pk      TEXT NOT NULL,
  at      REAL NOT NULL,
  deleted INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (tbl, pk)
);
CREATE INDEX IF NOT EXISTS ix_qf_sync_rows_at ON qf_sync_rows(at);
CREATE TABLE IF NOT EXISTS qf_sync_state (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS qf_sync_parents (
  child_uid  TEXT PRIMARY KEY,
  parent_uid TEXT NOT NULL
);
"""

#: 按 `qf_sync_parents` 补 `messages.parent_id`。
#:
#: 为什么要把映射**落盘**：拉取按 `at` 排序，父消息完全可能在后面的页、甚至后面的
#: 轮次才到。原先只在"同一页内"回填一次，跨页的就永远补不上 —— 实测一篇 53 条的
#: 对话里 23 条 `parent_id` 为空，对话树整个散掉（用户看到的就是"结构乱了"）。
_BACKFILL_PARENTS_SQL = (
    "UPDATE messages SET parent_id = ("
    "  SELECT p.id FROM messages p"
    "  JOIN qf_sync_parents r ON r.parent_uid = p.uid"
    "  WHERE r.child_uid = messages.uid"
    ")"
    " WHERE parent_id IS NULL"
    "   AND uid IN (SELECT child_uid FROM qf_sync_parents)"
)

#: 触发器版本：改了触发器 SQL 就把 1 改成 2（`IF NOT EXISTS` 不会更新旧定义）。
_TRIGGER_VERSION = 1

#: 推送：一页多少行（每行两条远端语句，连 BEGIN/COMMIT 一起算请求数）。
_PUSH_PAGE = 100
#: 增量拉取：一页多少条日志。
_PULL_PAGE = 500
#: 全量镜像：一张表一页多少行。
_FULL_PAGE = 200

_AT_EXPR = (
    "MAX((julianday('now') - 2440587.5) * 86400.0,"
    " COALESCE((SELECT MAX(at) FROM qf_sync_rows), 0.0) + 0.000001)"
)

_PASS_ENVS = ("QF_SYNC_PASSPHRASE", "QUIZFORGE_SNAPSHOT_PASSPHRASE")
_SCHEMA_VERSION = "1"

_run_lock = threading.Lock()
_running = False
_last_error: str | None = None


class SyncError(RuntimeError):
    """同步本身的问题（没配置、密钥对不上、远端拒绝……）。"""


class SyncNotConfigured(SyncError):
    """设置里还没有 Turso 的地址/令牌。"""


class SyncOfflineError(SyncError):
    """**网络层**就没通：DNS / 连接 / TLS / 超时。

    与"服务器回了错"必须分开（2026-11-11 用户要求）：前者的下一步是检查网络，
    后者的下一步是检查令牌或稍后再试。糊成一句"同步失败"等于让人两边乱试。
    """


class SyncHTTPError(SyncError):
    """服务器**回了响应**，但状态码不是 2xx。`status` 带原始码，供界面分诊。"""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = int(status)


def _classify_error(exc: BaseException) -> str:
    """失败 → 一个给界面用的类别（见前端 `syncFailText`）。

    值：`offline`（网络没通）/ `auth`（令牌被拒）/ `server`（对面 5xx）/
    `request`（对面 4xx，请求本身有问题）/ `config`（本机没配）/ `other`（剩下的）。

    **没有 `key` 这一类**（2026-11-11 用户："加密应该是秘密进行的"）：密钥相关的
    失败不该有独立文案，界面上也不该出现"加密"两个字 —— 它落进 `other`，
    文案就是那条中性的事故说明。
    """
    if isinstance(exc, SyncNotConfigured):
        return "config"
    if isinstance(exc, SyncOfflineError):
        return "offline"
    if isinstance(exc, SyncHTTPError):
        if exc.status in (401, 403):
            return "auth"
        return "server" if exc.status >= 500 else "request"
    # 传输层错误可能被外面又包了一层（`raise ... from exc`）：`__cause__` 也要看
    cause = exc.__cause__ or exc.__context__
    if isinstance(exc, httpx.TransportError) or isinstance(cause, httpx.TransportError):
        return "offline"
    text = _error_text(exc)
    if "连不上 Turso" in text:
        return "offline"
    if "Turso 返回 401" in text or "Turso 返回 403" in text:
        return "auth"
    return "other"


def _error_text(exc: BaseException) -> str:
    """异常 → 给用户看的一句话。

    **不能只有 `str(exc)`**（2026-11-11 实测）：网络层抛上来的
    `httpx.ConnectError` 有时 `str()` 是空的，界面于是显示
    「连不上 Turso（https://…）：」—— 冒号后面什么都没有，等于没说。
    空的时候退回异常类型名，至少让人知道是哪一层坏的。
    """
    text = str(exc).strip()
    return text or (type(exc).__name__ + "（底层没有给出细节）")


def _record_failure(engine: Engine, exc: BaseException) -> None:
    """把这一轮失败写进 `last_result`（设置页读的就是它）。

    为什么要独立成函数：有两处需要（`run_once` 的 except、后台线程的兜底），
    而**兜底那一处是必须的** —— `TursoClient(...)` 的构造曾经在 `run_once` 的
    try 之外，构造一抛（配置坏了、环境缺依赖），`last_result` 根本不更新，
    设置页继续显示上一次的「推 0 / 拉 0」，用户以为同步成功了。

    `kind` 写进去是为了让界面能说清**是哪一种失败**（网络 / 令牌 / 对面 5xx /
    本机没配 / 密钥），而不是笼统一句"同步失败"。
    """
    try:
        with engine.begin() as conn:
            _state_set(
                conn,
                "last_result",
                json.dumps(
                    {
                        "ok": False,
                        "at": _now_iso(),
                        "error": _error_text(exc),
                        "kind": _classify_error(exc),
                    },
                    ensure_ascii=False,
                ),
            )
    except Exception:  # noqa: BLE001 - 写状态失败不该盖掉真正的错
        log.warning("同步失败的状态没写进库", exc_info=True)


# ============================================================================ 表规格


@dataclass(frozen=True)
class TableSpec:
    name: str
    key: tuple[str, ...]
    #: 本机生成、不参与同步的列（自增 id）。
    generated: tuple[str, ...] = ()
    private: bool = False
    #: 基线用的时间列（按顺序取第一个能解析的）。
    timestamp_columns: tuple[str, ...] = ()
    #: 只接受这些列（records 的计数列由流水维护，不能按远端整行覆盖）。
    subjective: tuple[str, ...] = ()


def _build_specs() -> dict[str, TableSpec]:
    out: dict[str, TableSpec] = {}
    for name in SYNC_TABLES:
        table = Base.metadata.tables.get(name)
        if table is None:
            raise SyncError(f"同步表 {name} 不在模型里")
        if name in ("messages", "conversation_folders"):
            # 自增 id 两台机器会撞号：用 uid 当同步身份，id 由本机重新发。
            key: tuple[str, ...] = ("uid",)
            generated: tuple[str, ...] = ("id",)
        else:
            key = tuple(col.name for col in table.primary_key.columns)
            generated = ()
        out[name] = TableSpec(
            name=name,
            key=key,
            generated=generated,
            private=name in PRIVATE_TABLES,
            timestamp_columns=_TIMESTAMPS.get(name, ()),
            subjective=("patch", "mastered", "flagged", "client_rev") if name == "records" else (),
        )
    return out


_TIMESTAMPS: dict[str, tuple[str, ...]] = {
    "app_settings": ("updated_at",),
    "my_questions": ("updated_at", "created_at"),
    "conversations": ("updated_at", "created_at"),
    "conversation_folders": ("created_at",),
    "messages": ("updated_at", "created_at"),
    "attachments": ("created_at",),
    "records": ("updated_at",),
    "attempts": ("at", "created_at"),
    "ai_usage": ("updated_at",),
    "memories": ("updated_at", "created_at"),
    "notes": ("updated_at",),
    "plans": ("updated_at",),
    "plan_items": ("updated_at",),
    "schedule_events": ("updated_at", "start_at"),
    "sleep_logs": ("updated_at", "bed_at"),
    "health_regimes": ("updated_at",),
    "schedule_runs": ("updated_at",),
    "gate_runs": ("updated_at",),
    "lens_records": ("updated_at", "created_at"),
    "lenses": ("updated_at", "created_at"),
}

SPECS: dict[str, TableSpec] = _build_specs()


# ============================================================================ 小工具


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _lit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _pk_json(values: list[Any]) -> str:
    return json.dumps(values, ensure_ascii=False, separators=(",", ":"))


def _key_expr(prefix: str, spec: TableSpec) -> str:
    return "json_array(" + ", ".join(f'{prefix}.{_q(col)}' for col in spec.key) + ")"


def _table(name: str):
    return Base.metadata.tables[name]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _to_epoch(value: Any) -> float | None:
    """把库里读到的时间值换算成 epoch 秒；认不出来就返回 None。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=UTC)
        return moment.timestamp()
    if isinstance(value, (int, float)):
        if value <= 0:
            return None
        # 大于这个量级的只可能是毫秒（records 的 last_at）
        return float(value) / 1000.0 if value > 10_000_000_000 else float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    raw = text.replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d").replace(tzinfo=UTC).timestamp()
        except ValueError:
            return None
    return (moment if moment.tzinfo else moment.replace(tzinfo=UTC)).timestamp()


def _state_get(conn, key: str, default: str | None = None) -> str | None:  # noqa: ANN001
    row = conn.exec_driver_sql("SELECT v FROM qf_sync_state WHERE k = ?", (key,)).fetchone()
    return str(row[0]) if row is not None else default


def _state_set(conn, key: str, value: Any) -> None:  # noqa: ANN001
    conn.exec_driver_sql(
        "INSERT INTO qf_sync_state(k, v) VALUES(?, ?)"
        " ON CONFLICT(k) DO UPDATE SET v = excluded.v",
        (key, str(value)),
    )


def _state_del(conn, key: str) -> None:  # noqa: ANN001
    conn.exec_driver_sql("DELETE FROM qf_sync_state WHERE k = ?", (key,))


# ============================================================================ 本地结构


def _trigger_sql(spec: TableSpec) -> list[str]:
    """三只触发器：INSERT / UPDATE / DELETE。

    `at` 先写进影子表、再**读回来**写进 journal：两条语句各算一次
    `julianday('now')` 会差几微秒，而推送要靠 `(tbl, pk, at)` 精确匹配
    来条件删除脏标记 —— 差一点就永远删不掉了。
    """
    tbl = _lit(spec.name)
    name = spec.name
    version = _TRIGGER_VERSION

    def put(kind: str, event: str, key_expr: str) -> str:
        return (
            f'CREATE TRIGGER IF NOT EXISTS "qf_sync{version}_{name}_{kind}"'
            f" AFTER {event} ON {_q(name)}\n"
            "WHEN NOT EXISTS (SELECT 1 FROM qf_sync_state WHERE k = 'paused')\n"
            "BEGIN\n"
            "  INSERT INTO qf_sync_rows (tbl, pk, at, deleted)\n"
            f"  VALUES ({tbl}, {key_expr}, {_AT_EXPR}, 0)\n"
            "  ON CONFLICT(tbl, pk) DO UPDATE SET at = excluded.at, deleted = 0;\n"
            "  INSERT INTO qf_sync_journal (tbl, pk, op, at)\n"
            f"  SELECT {tbl}, pk, 'put', at FROM qf_sync_rows"
            f" WHERE tbl = {tbl} AND pk = {key_expr}\n"
            "  ON CONFLICT(tbl, pk) DO UPDATE SET op = excluded.op, at = excluded.at;\n"
            "END"
        )

    new_key = _key_expr("NEW", spec)
    old_key = _key_expr("OLD", spec)
    delete = (
        f'CREATE TRIGGER IF NOT EXISTS "qf_sync{version}_{name}_delete"'
        f" AFTER DELETE ON {_q(name)}\n"
        "WHEN NOT EXISTS (SELECT 1 FROM qf_sync_state WHERE k = 'paused')\n"
        "BEGIN\n"
        "  INSERT INTO qf_sync_rows (tbl, pk, at, deleted)\n"
        f"  VALUES ({tbl}, {old_key}, {_AT_EXPR}, 1)\n"
        "  ON CONFLICT(tbl, pk) DO UPDATE SET at = excluded.at, deleted = 1;\n"
        "  INSERT INTO qf_sync_journal (tbl, pk, op, at)\n"
        f"  SELECT {tbl}, pk, 'del', at FROM qf_sync_rows"
        f" WHERE tbl = {tbl} AND pk = {old_key}\n"
        "  ON CONFLICT(tbl, pk) DO UPDATE SET op = excluded.op, at = excluded.at;\n"
        "END"
    )
    return [
        put("insert", "INSERT", new_key),
        put("update", "UPDATE", new_key),
        delete,
    ]


def ensure_sync_schema(engine: Engine | None = None) -> None:
    """建本地同步表与触发器（幂等）。每次同步/查状态前都跑一遍。

    为什么不在 `db.ensure_schema` 里只跑一次：测试会 `drop_all/create_all`
    （触发器跟着表一起没了），而且以后加表时这里要能补齐。代价只是一串
    `CREATE ... IF NOT EXISTS`。
    """
    engine = engine or get_engine()
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA busy_timeout=5000")
        for statement in _LOCAL_SCHEMA.strip().split(";\n"):
            statement = statement.strip()
            if statement:
                conn.exec_driver_sql(statement)
        # 老库的自增 id 表可能还没有 uid（补列在 db._ensure_columns，补值在 _BACKFILLS）。
        for table, column in (("messages", "uid"), ("conversation_folders", "uid")):
            if _has_table(conn, table) and _has_column(conn, table, column):
                conn.exec_driver_sql(
                    f"CREATE INDEX IF NOT EXISTS ix_{table}_uid ON {_q(table)} ({_q(column)})"
                )
                # 先看一眼再写：状态页每 2 秒轮询都会走到这里，而一句"0 行也要
                # 拿写锁"的全表 UPDATE 足够和同步线程撞出 `database is locked`。
                blank = conn.exec_driver_sql(
                    f"SELECT 1 FROM {_q(table)}"
                    f" WHERE {_q(column)} IS NULL OR {_q(column)} = '' LIMIT 1"
                ).fetchone()
                if blank is not None:
                    conn.exec_driver_sql(
                        f"UPDATE {_q(table)} SET {_q(column)} = lower(hex(randomblob(16)))"
                        f" WHERE {_q(column)} IS NULL OR {_q(column)} = ''"
                    )
        for name in SYNC_TABLES:
            if not _has_table(conn, name):
                continue
            for statement in _trigger_sql(SPECS[name]):
                conn.exec_driver_sql(statement)


def _has_table(conn, name: str) -> bool:  # noqa: ANN001
    row = conn.exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _has_column(conn, table: str, column: str) -> bool:  # noqa: ANN001
    return any(str(row[1]) == column for row in conn.exec_driver_sql(
        f"PRAGMA table_info({_q(table)})"
    ))


# ============================================================================ 密钥


def _derived_passphrase() -> str:
    """密钥从 **Turso 的地址 + 令牌**推出来，不再由人保管。

    用户 2026-11-10："这个数据的密钥其实和用户与 turso 都没关系，不该暴露给
    用户。数据库是用户私有的，那么可以认为数据库地址 + token 已经足以表明
    用户的身份" —— 于是口令退化成一份**推得出来的**东西：同一组 (url, token)
    在任何设备上得到同一串，"两台机器必须同一份"这条约束自动成立。

    代价（明确接受）：**令牌一换，历史密文就解不开了**。换令牌属于迁移操作，
    不是日常操作。
    """
    conf = turso_config()
    url = str(conf.get("url") or "").strip().rstrip("/")
    token = str(conf.get("token") or "").strip()
    if not url or not token:
        return ""
    material = f"quizforge-sync-key-v1:{url}".encode()
    return base64.b64encode(
        hmac.new(token.encode("utf-8"), material, hashlib.sha256).digest()
    ).decode("ascii")


def _passphrase() -> str:
    """口令：环境变量（测试/救援）→ 由 turso 凭据派生。

    2026-11-10 起**不再读**设置文件里的 `syncPassphrase`：那是旧模型
    （人手保管一份口令）的遗留。留着它只会让两台机器不一致，而且它本来
    就不该出现在用户面前。
    """
    for name in _PASS_ENVS:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return _derived_passphrase()


def passphrase() -> str:
    """派生出来的那串。设置页不再展示它（用户不需要知道）。

    保留这个函数是给 `/sync/status` 判 `keyReady` 与 Obsidian 插件探测用。
    """
    return _passphrase()


_key_cache: dict[tuple[bytes, bytes], bytes] = {}

#: 一个进程共用的 HTTP 连接。Turso 每次同步要发不少条 pipeline；
#: 每条都新建连接在 WSL 的 loopback 上会撞 40ms 的延迟确认，测试里能差出一倍。
#:
#: **空闲连接 20 秒就过期**（2026-11-13 踩的坑）。原先用默认值（永不主动过期），
#: 于是在 WSL 上开关 Clash / 换代理时，池子里那几条 keepalive 连接会被代理侧
#: 静默丢掉，而客户端还以为它们活着 —— 之后每一次同步都复用在死连接上，卡满
#: 45 秒超时后抛 `PoolTimeout`，一坏就是几小时（重启进程才好）。
#: 同步本身是 30 秒一次的轮询，20 秒过期等于"跨轮一律不复用、同一轮里照样复用"，
#: 代价是一两次握手，换来的是网络抖一下不会把整个通道永久打死。
_http = httpx.Client(
    follow_redirects=False,
    limits=httpx.Limits(max_keepalive_connections=8, keepalive_expiry=20.0),
)


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    # 缓存按「盐 + 口令」：只按盐会把上一次口令派生的钥匙错给这一次
    # （口令换了却还在同一个进程里跑同步时，校验值会莫名其妙对不上）。
    tag = (salt, hashlib.sha256(passphrase.encode("utf-8")).digest())
    cached = _key_cache.get(tag)
    if cached is not None:
        return cached
    key = Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(passphrase.encode("utf-8"))
    if len(_key_cache) >= 8:
        _key_cache.clear()
    _key_cache[tag] = key
    return key


def _key_check(key: bytes) -> str:
    return base64.b64encode(
        hmac.new(key, b"quizforge-sync-v1", hashlib.sha256).digest()
    ).decode("ascii")


def _aad(tbl: str, pk: str) -> bytes:
    return f"quizforge-sync:v1:{tbl}:{pk}".encode("utf-8")


def _encrypt(key: bytes, tbl: str, pk: str, data: dict) -> str:
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    nonce = os.urandom(12)
    sealed = AESGCM(key).encrypt(nonce, raw, _aad(tbl, pk))
    return "v1:" + base64.b64encode(nonce + sealed).decode("ascii")


def _decrypt(key: bytes, tbl: str, pk: str, payload: str) -> dict:
    raw = payload[3:] if payload.startswith("v1:") else payload
    try:
        blob = base64.b64decode(raw)
        data = AESGCM(key).decrypt(blob[:12], blob[12:], _aad(tbl, pk))
    except (InvalidTag, ValueError) as exc:
        raise SyncError(
            f"解不开 {tbl}/{pk}：两台机器的数据库凭据不是同一套（或密文已过期）。"
        ) from exc
    decoded = json.loads(data.decode("utf-8"))
    if not isinstance(decoded, dict):
        raise SyncError(f"{tbl}/{pk} 的密文内容不是一个对象")
    return decoded


# ============================================================================ Turso 客户端


def turso_config() -> dict:
    """Turso 的地址与令牌。

    **读出来就 trim**（2026-11-11 用户要求）：粘贴令牌时首尾很容易带空格/换行，
    而它进了 `Authorization: Bearer …` 就是 401 —— 用户对着一段看不见空格的字符串
    根本查不出来。这里直接修掉，别把这种错留给界面去解释。
    """
    raw = settings_file.load().get("turso")
    if not isinstance(raw, dict):
        return {}
    conf = dict(raw)
    for key in ("url", "token"):
        if isinstance(conf.get(key), str):
            conf[key] = conf[key].strip()
    return conf


def turso_configured() -> bool:
    conf = turso_config()
    return bool(str(conf.get("url") or "").strip() and str(conf.get("token") or "").strip())


def turso_enabled() -> bool:
    conf = turso_config()
    return conf.get("enabled") is not False


def endpoint_for(url: str) -> str:
    """`libsql://…` → `https://…/v2/pipeline`（Turso 的 Hrana HTTP 入口）。"""
    raw = str(url or "").strip().rstrip("/")
    if not raw:
        raise SyncNotConfigured("没配 Turso 数据库地址")
    if raw.startswith("libsql://"):
        raw = "https://" + raw[len("libsql://"):]
    if not raw.startswith(("http://", "https://")):
        raise SyncNotConfigured(f"Turso 地址不认识：{raw}（要 libsql:// 或 https://）")
    if not raw.endswith("/v2/pipeline"):
        raw += "/v2/pipeline"
    return raw


def _arg(value: Any) -> dict:
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "integer", "value": "1" if value else "0"}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    if isinstance(value, (bytes, bytearray)):
        return {"type": "blob", "base64": base64.b64encode(bytes(value)).decode("ascii")}
    return {"type": "text", "value": str(value)}


def _cell(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    kind = value.get("type")
    raw = value.get("value")
    if kind == "null":
        return None
    if kind == "integer":
        return int(raw)
    if kind == "float":
        return float(raw)
    if kind == "blob":
        return base64.b64decode(raw or "")
    return raw


class TursoClient:
    """够用就好的 Hrana HTTP 客户端（只走 `/v2/pipeline`，不引新依赖）。"""

    def __init__(self, url: str, token: str, *, timeout: float = 45.0) -> None:
        self.endpoint = endpoint_for(url)
        self.token = token
        self.timeout = timeout

    def pipeline(self, requests: list[dict]) -> list[dict]:
        body = {"requests": [*requests, {"type": "close"}]}
        try:
            response = _http.post(
                self.endpoint,
                json=body,
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise SyncOfflineError(
                f"连不上 Turso（{self.endpoint}）：{_error_text(exc)}"
            ) from exc
        if response.status_code >= 400:
            detail = response.text[:300]
            raise SyncHTTPError(
                f"Turso 返回 {response.status_code}：{detail}", response.status_code
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise SyncError("Turso 的响应不是 JSON") from exc
        results = payload.get("results") or []
        for result in results:
            if result.get("type") == "error":
                raise SyncError("Turso 拒绝了语句：" + str((result.get("error") or {}).get("message"))[:400])
        return results

    def query(self, sql: str, args: tuple[Any, ...] = ()) -> list[dict]:
        results = self.pipeline(
            [{"type": "execute", "stmt": {"sql": sql, "args": [_arg(a) for a in args], "want_rows": True}}]
        )
        if not results:
            return []
        result = (results[0].get("response") or {}).get("result") or {}
        names = [col.get("name") for col in result.get("cols") or []]
        return [
            {names[i]: _cell(cell) for i, cell in enumerate(row)}
            for row in result.get("rows") or []
        ]

    def execute(self, sql: str, args: tuple[Any, ...] = ()) -> None:
        self.pipeline([{"type": "execute", "stmt": {"sql": sql, "args": [_arg(a) for a in args]}}])

    #: 一批最多多少字节。实测：首次同步一次塞了 6.9MB（202 条 execute），
    #: 请求被中途断掉（先是 `Connection reset by peer`，再试是
    #: `SSL: UNEXPECTED_EOF_WHILE_READING`）—— 走代理与直连都一样。
    #: 512KB 在代理与 WSL loopback 上都稳，多切几批的代价可以忽略。
    BATCH_BYTES = 512 * 1024

    def batch(self, statements: list[tuple[str, tuple[Any, ...]]]) -> None:
        """按字节切成若干事务跑；每条都是幂等的（INSERT OR REPLACE），
        所以中途断了重推也不会写重。"""
        chunk: list[tuple[str, tuple[Any, ...]]] = []
        size = 0
        for sql, args in statements:
            one = len(sql.encode("utf-8")) + 64
            for value in args:
                one += len(json.dumps(_arg(value), ensure_ascii=False).encode("utf-8"))
            if chunk and size + one > self.BATCH_BYTES:
                self._batch_one(chunk)
                chunk, size = [], 0
            chunk.append((sql, args))
            size += one
        if chunk:
            self._batch_one(chunk)

    def _batch_one(self, statements: list[tuple[str, tuple[Any, ...]]]) -> None:
        """一个事务里跑完这一批；任何一条失败就整批不落。"""
        requests: list[dict] = [{"type": "execute", "stmt": {"sql": "BEGIN"}}]
        requests += [
            {"type": "execute", "stmt": {"sql": sql, "args": [_arg(a) for a in args]}}
            for sql, args in statements
        ]
        requests.append({"type": "execute", "stmt": {"sql": "COMMIT"}})
        self.pipeline(requests)

    # ---- 远端结构 ---------------------------------------------------------

    def ensure_schema(self) -> None:
        requests = [
            {"type": "execute", "stmt": {"sql": statement.strip()}}
            for statement in _REMOTE_SCHEMA.strip().split(";\n")
            if statement.strip()
        ]
        self.pipeline(requests)

    def meta(self) -> dict[str, str]:
        rows = self.query("SELECT k, v FROM qf_sync_meta")
        return {str(row["k"]): str(row["v"]) for row in rows}


# ============================================================================ 远端密钥


def _remote_key(remote: Any, *, need_private: bool) -> bytes | None:
    """读（或首次写入）远端盐，派生密钥并核对校验值。

    `need_private=False` 且远端还没有盐时，不创建 —— 一台只同步公开数据的
    机器不该替别人选定口令。
    """
    meta = remote.meta()
    salt_b64 = meta.get("salt")
    check = meta.get("check")
    passphrase = _passphrase()

    if not salt_b64:
        if not passphrase:
            if need_private:
                # 不提加密/密钥（2026-11-11 用户："加密应该是秘密进行的"）：
                # 对人只需要说"缺地址与令牌、去哪填"。
                raise SyncError(
                    "要同步对话 / 记忆 / 计划书，得先配好 Turso 的地址与令牌。\n"
                    "  在「设置 → 同步」里填上（或写进 config/settings.local.json 的 turso 块）。"
                )
            return None
        salt = os.urandom(16)
        key = _derive_key(passphrase, salt)
        remote.batch(
            [
                (
                    "INSERT OR IGNORE INTO qf_sync_meta(k, v) VALUES('salt', ?),('check', ?),('version', ?)",
                    (
                        base64.b64encode(salt).decode("ascii"),
                        _key_check(key),
                        _SCHEMA_VERSION,
                    ),
                )
            ]
        )
        meta = remote.meta()
        salt_b64 = meta.get("salt")
        check = meta.get("check")
        if not salt_b64:
            raise SyncError("远端没记住刚写进去的盐，稍后重试")

    if not passphrase:
        if need_private:
            raise SyncError(
                "远端已经有对话 / 记忆 / 计划书，而本机还缺 Turso 的地址或令牌。\n"
                "  在「设置 → 同步」里填上就能对齐。"
            )
        return None

    key = _derive_key(passphrase, base64.b64decode(salt_b64))
    # 旧库（2026-11-10 之前）没有 `key` 标记，check 是按人手口令算的 —— 那一代
    # 校验值现在必然对不上，但这是**预期的**：`_migrate_key()` 会重写它并把
    # 所有加密行重推（用户："现有密文作废，全部重新加密"）。所以只在已经迁到
    # 新算法之后才拿 check 判"两台机器的令牌是不是同一套"。
    if check and meta.get(_KEY_TAG_META) == _KEY_TAG and not hmac.compare_digest(
        check, _key_check(key)
    ):
        raise SyncError(
            "两台机器的 Turso 凭据不是同一套。\n"
            "  地址和令牌必须指向同一个库；换过令牌的，早些时候同步过去的内容读不回来。"
        )
    return key


# ============================================================================ 本地行读写


def _row_of(conn, spec: TableSpec, values: list[Any]) -> dict | None:  # noqa: ANN001
    where = " AND ".join(f"{_q(col)} = ?" for col in spec.key)
    row = conn.exec_driver_sql(
        f"SELECT * FROM {_q(spec.name)} WHERE {where}", tuple(values)
    ).mappings().first()
    return dict(row) if row is not None else None


def _uid_for(conn, table: str, local_id: Any, id_column: str = "id") -> str | None:  # noqa: ANN001
    if local_id in (None, ""):
        return None
    row = conn.exec_driver_sql(
        f"SELECT uid FROM {_q(table)} WHERE {_q(id_column)} = ?", (local_id,)
    ).fetchone()
    return str(row[0]) if row is not None and row[0] else None


def _local_id_for_uid(conn, table: str, uid: Any) -> Any:  # noqa: ANN001
    if not uid:
        return None
    row = conn.exec_driver_sql(
        f"SELECT id FROM {_q(table)} WHERE uid = ?", (str(uid),)
    ).fetchone()
    return row[0] if row is not None else None


def _payload_of(conn, spec: TableSpec, values: list[Any]) -> dict | None:  # noqa: ANN001
    row = _row_of(conn, spec, values)
    if row is None:
        return None
    data = dict(row)
    for column in spec.generated:
        data.pop(column, None)
    if spec.name == "messages":
        parent = data.pop("parent_id", None)
        data["parent_uid"] = _uid_for(conn, "messages", parent)
    elif spec.name == "attachments":
        message = data.pop("message_id", None)
        data["message_uid"] = _uid_for(conn, "messages", message)
    elif spec.name == "app_settings":
        data["data"] = _scrub_settings(data.get("data"))
    elif spec.name == "records":
        # 计数由流水累加，但历史上有"只有计数、没有流水"的行（导入的进度）。
        # 把差额随行带上：接收方从差额起算，再拉流水，既不会算两遍也不丢旧账。
        data["sync_delta"] = _record_delta(conn, data)
    return data


def _record_delta(conn, data: dict) -> dict[str, int]:  # noqa: ANN001
    """records 里**没有对应流水**的那部分计数。"""
    counts = {"attempts": 0, "correct": 0, "partial": 0, "wrong": 0}
    question_id = str(data.get("question_id") or "")
    if question_id:
        rows = conn.exec_driver_sql(
            "SELECT status, COUNT(*) FROM attempts WHERE question_id = ? GROUP BY status",
            (question_id,),
        )
        for status, count in rows:
            counts["attempts"] += int(count)
            column = str(status)
            if column in ("correct", "partial", "wrong"):
                counts[column] += int(count)
    return {
        key: max(0, int(data.get(key) or 0) - counts[key])
        for key in ("attempts", "correct", "partial", "wrong")
    }


def _json_dict(raw: Any) -> dict:
    """SQLite 的 JSON 列读出来是 TEXT —— 同步两侧都按 dict 处理。"""
    if isinstance(raw, (str, bytes, bytearray)):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return {}
    return raw if isinstance(raw, dict) else {}


def _scrub_settings(raw: Any) -> Any:
    """上传前抹掉本机凭据（密钥 / Turso 令牌）。"""
    if not isinstance(raw, dict):
        raw = _json_dict(raw)
    data = json.loads(json.dumps(raw, ensure_ascii=False))  # 深拷贝
    for node, leaf in _SECRET_PATHS:
        branch = data.get(node)
        if isinstance(branch, dict) and branch.get(leaf):
            branch[leaf] = ""
    for block in _SCRUB_SETTINGS_BLOCKS:
        data.pop(block, None)          # turso 整块（含 token 与地址）不上传
    # 只在本机的状态（读到哪儿 / 面板位置 / 选题篮）也不上传；滤镜旧键顺手并进 lens
    return normalize(data)


def _merge_settings(local: Any, remote: Any) -> dict:
    """远端设置合到本机：秘密与机器属性保留本机的，其余按远端。"""
    local = _json_dict(local)
    remote = _json_dict(remote)
    merged = json.loads(json.dumps(remote, ensure_ascii=False))
    merged.pop("syncPassphrase", None)   # 口令永远以本机 key 文件为准
    for block in _MACHINE_BLOCKS:
        if block in local:
            merged[block] = local[block]
        else:
            merged.pop(block, None)
    for node, leaf in _SECRET_PATHS:
        branch = local.get(node)
        value = branch.get(leaf) if isinstance(branch, dict) else None
        if value:
            target = merged.setdefault(node, {})
            if isinstance(target, dict):
                target[leaf] = value
    # 老客户端推上来的设置可能还带本机状态键：落地之前滤掉
    return normalize(merged)


# ============================================================================ 基线


def _baseline(engine: Engine) -> int:
    """给"触发器出生之前就存在"的行补影子与脏标记。

    **不是"整库只做一次"** —— 老写法是一个 `baseline_done=1` 一刀切，代价写实：
    `SYNC_TABLES` 后来长出来的表（日程、计划条目、健康都是后加的）如果在第一次
    同步之后才有行，就永远进不了队列。实测：181 条日程与 9 条计划条目在本机
    躺了一整天，远端一行都没有 —— 而两边看起来都"同步成功"（推送 316 行、
    状态全绿），因为缺失的正是**没有被记脏的那些表**。

    现在的口径：按表登记 + 每次核对"本地行数 vs 影子行数"。
    对不上（新表 / 触发器没建 / 老数据）就补一次；数字相等时只花两条 COUNT。
    """
    with engine.begin() as conn:
        done_raw = _state_get(conn, _BASELINE_STATE_KEY) or ""
        done = {name for name in done_raw.split(",") if name}
        todo: list[str] = []
        for name in SYNC_TABLES:
            if name not in done:
                todo.append(name)
                continue
            local = conn.exec_driver_sql(f"SELECT COUNT(*) FROM {_q(name)}").fetchone()[0]
            shadow = conn.exec_driver_sql(
                "SELECT COUNT(*) FROM qf_sync_rows WHERE tbl = ? AND deleted = 0", (name,)
            ).fetchone()[0]
            if local != shadow:
                todo.append(name)
        if not todo:
            return 0
        added = 0
        for name in todo:
            spec = SPECS[name]
            rows = conn.exec_driver_sql(f"SELECT * FROM {_q(name)}").mappings().all()
            for raw in rows:
                row = dict(raw)
                values = [row.get(col) for col in spec.key]
                pk = _pk_json(values)
                exists = conn.exec_driver_sql(
                    "SELECT 1 FROM qf_sync_rows WHERE tbl = ? AND pk = ?", (name, pk)
                ).fetchone()
                if exists is not None:
                    continue           # 触发器已经记过了
                at = 0.0
                for column in spec.timestamp_columns:
                    stamp = _to_epoch(row.get(column))
                    if stamp is not None:
                        at = stamp
                        break
                conn.exec_driver_sql(
                    "INSERT INTO qf_sync_rows(tbl, pk, at, deleted) VALUES(?, ?, ?, 0)",
                    (name, pk, at),
                )
                conn.exec_driver_sql(
                    "INSERT INTO qf_sync_journal(tbl, pk, op, at) VALUES(?, ?, 'put', ?)"
                    " ON CONFLICT(tbl, pk) DO UPDATE SET op = excluded.op, at = excluded.at",
                    (name, pk, at),
                )
                added += 1
        _state_set(conn, _BASELINE_STATE_KEY, ",".join(sorted(done | set(todo))))
        _state_set(conn, "baseline_done", "1")
        return added


def _reschedule_tables(engine: Engine, names: Iterable[str]) -> int:
    """把本地这些表的**每一行**抬到"现在"并记脏，逼下一次推送重发。

    本地表存的是明文（加密只发生在推送那一刻），所以"重发"就等于"用当前
    密钥重新封一遍"。两件事共用这一条：表后来才划进加密范围（换明文为密文）、
    密钥算法换代（换旧密钥为新密钥）。

    返回排进推送队列的行数。
    """
    with engine.begin() as conn:
        scheduled = 0
        for name in names:
            spec = SPECS.get(name)
            if spec is None:
                continue
            columns = ", ".join(_q(col) for col in spec.key)
            rows = conn.exec_driver_sql(f"SELECT {columns} FROM {_q(name)}").fetchall()
            for values in rows:
                pk = _pk_json(list(values))
                shadow = conn.exec_driver_sql(
                    "SELECT at, deleted FROM qf_sync_rows WHERE tbl = ? AND pk = ?",
                    (name, pk),
                ).fetchone()
                if shadow is not None and int(shadow[1]):
                    # 影子是"已删除"，本地却还有这一行：别借重推把它复活。
                    # 正常流程不会出现（删除会连本地行一起删）。
                    continue
                previous = float(shadow[0]) if shadow is not None else 0.0
                at = max(time.time(), previous + 0.000001)
                conn.exec_driver_sql(
                    "INSERT INTO qf_sync_rows(tbl, pk, at, deleted) VALUES(?, ?, ?, 0)"
                    " ON CONFLICT(tbl, pk) DO UPDATE SET at = excluded.at, deleted = 0",
                    (name, pk, at),
                )
                conn.exec_driver_sql(
                    "INSERT INTO qf_sync_journal(tbl, pk, op, at) VALUES(?, ?, 'put', ?)"
                    " ON CONFLICT(tbl, pk) DO UPDATE SET op = 'put', at = excluded.at",
                    (name, pk, at),
                )
                scheduled += 1
        return scheduled


def _schedule_private_rescope(engine: Engine) -> int:
    """把**后来才划进加密范围**的表整表重推一遍；每台机器只做一次。

    `PRIVATE_TABLES` 决定的是**推送时**加不加密。表在划进来之前已经同步过
    的行，远端存的是明文；只改常量不会回头重写它们。这里把本机仍在的每一行
    影子时间戳抬到"现在"并记脏，下一次推送就会拿密文覆盖远端那一行。

    顺带兜住另一种漏：表可能是在 `baseline_done` 之后才加的，建表到第一次
    同步之间写入的行没有影子 —— 这里一并补出来。

    返回这次排进推送队列的行数（0 = 早就做过，或这些表还是空的）。
    """
    with engine.begin() as conn:
        done_raw = _state_get(conn, _RESCOPE_STATE_KEY) or ""
        done = {name for name in done_raw.split(",") if name}
        todo = [name for name in _RESCOPE_PRIVATE if name in SPECS and name not in done]
        if not todo:
            return 0
    scheduled = _reschedule_tables(engine, todo)
    with engine.begin() as conn:
        _state_set(conn, _RESCOPE_STATE_KEY, ",".join(sorted(done | set(todo))))
    return scheduled


def _migrate_key(engine: Engine, remote: Any, key: bytes) -> int:
    """密钥换代（2026-11-10）：远端校验值重写 + 本地加密行整表重推。

    为什么不需要"先用旧口令解密再加密"：本地表里存的一直是明文，密文只在
    远端。用户也明确说了旧密文作废（"现有密文作废，全部重新加密"），所以
    这里只做两件事：把 `check` 换成新密钥的、把本机 private 行全部标脏 ——
    下一次推送即用新密钥覆盖远端的每一行。

    一次性：`qf_sync_meta.key` 就是那个开关（旧库没有这个键）。
    """
    meta = remote.meta()
    if meta.get(_KEY_TAG_META) == _KEY_TAG:
        return 0
    remote.batch(
        [
            (
                "INSERT INTO qf_sync_meta(k, v) VALUES('check', ?)"
                " ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                (_key_check(key),),
            ),
            (
                "INSERT INTO qf_sync_meta(k, v) VALUES(?, ?)"
                " ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                (_KEY_TAG_META, _KEY_TAG),
            ),
        ]
    )
    return _reschedule_tables(engine, PRIVATE_TABLES)


# ============================================================================ 推送


def _parent_uids_from_remote(remote: Any, key: bytes | None, pks: list[str]) -> dict[str, str]:
    """读远端镜像里这些 pk 的 `parent_uid`（只返回非空的）。

    推送前的护栏用：本机某条消息没有父、而远端镜像有 —— 说明本地这一份是坏的
    （断链），**不能拿空值去覆盖远端**。见 `_push` 里的说明。
    """
    out: dict[str, str] = {}
    for start in range(0, len(pks), 100):
        chunk = pks[start : start + 100]
        rows = remote.query(
            "SELECT pk, enc, payload FROM qf_sync_rows WHERE tbl = 'messages' AND pk IN ("
            + ", ".join("?" for _ in chunk)
            + ")",
            tuple(chunk),
        )
        for row in rows:
            if row.get("payload") is None:
                continue
            try:
                if int(row.get("enc") or 0):
                    if key is None:
                        continue
                    data = _decrypt(key, "messages", str(row["pk"]), str(row["payload"]))
                else:
                    data = json.loads(str(row["payload"]))
            except (SyncError, ValueError, TypeError):
                continue
            parent = data.get("parent_uid")
            if parent:
                out[str(row["pk"])] = str(parent)
    return out


def _push(engine: Engine, remote: Any, key: bytes | None, origin: str) -> dict:
    pushed = 0
    pages = 0
    while pages < 10_000:
        pages += 1
        with engine.connect() as conn:
            entries = conn.exec_driver_sql(
                "SELECT tbl, pk, op, at FROM qf_sync_journal ORDER BY at LIMIT ?",
                (_PUSH_PAGE,),
            ).fetchall()
            if not entries:
                break
            staged: list[dict] = []
            deletes: list[tuple[str, str, float]] = []
            orphan_at: list[int] = []
            for tbl, pk, _op, _journal_at in entries:
                spec = SPECS.get(str(tbl))
                if spec is None:
                    # 表不在同步范围（旧版本留下的脏行）：直接丢掉
                    deletes.append((str(tbl), str(pk), float(_journal_at)))
                    continue
                values = json.loads(str(pk))
                shadow = conn.exec_driver_sql(
                    "SELECT at, deleted FROM qf_sync_rows WHERE tbl = ? AND pk = ?",
                    (tbl, pk),
                ).fetchone()
                if shadow is None:
                    deletes.append((str(tbl), str(pk), float(_journal_at)))
                    continue
                at = float(shadow[0])
                deleted = bool(shadow[1])
                payload: dict | None = None
                if not deleted:
                    payload = _payload_of(conn, spec, values)
                    if payload is None:
                        deleted = True     # 行没了：按删除传
                if spec.private and not deleted and key is None:
                    raise SyncError(
                        f"要上传加密的 {tbl}，但本机还没配 Turso 的地址与令牌"
                        "（密钥由它们推导）；填在「设置 → 同步」里。"
                    )
                if not deleted and spec.name == "messages" and not (payload or {}).get("parent_uid"):
                    orphan_at.append(len(staged))
                staged.append(
                    {
                        "spec": spec,
                        "tbl": str(tbl),
                        "pk": str(pk),
                        "at": at,
                        "deleted": deleted,
                        "payload": payload,
                    }
                )

        # ---- 护栏：父指针**只升不降**（2026-11-09 的事故防线）----------------
        # 一台本地 `parent_id` 全空的机器整表重推，会把远端已经正确的
        # `parent_uid` 全部洗成 null（日志 seq 3468–4084，617 条）；那台机器再拉
        # 回来，两边的树一起散掉。所以这里先看远端镜像：**它有的父，本机空着就沿用它**。
        # 代价：本机真把一条消息挪回根上，这次推送不会生效（应用里没有这种操作）。
        if orphan_at:
            rescued = _parent_uids_from_remote(
                remote, key, [str(staged[i]["pk"]) for i in orphan_at]
            )
            for i in orphan_at:
                parent = rescued.get(str(staged[i]["pk"]))
                if parent and staged[i]["payload"] is not None:
                    staged[i]["payload"]["parent_uid"] = parent

        statements: list[tuple[str, tuple[Any, ...]]] = []
        for item in staged:
            spec, tbl, pk = item["spec"], item["tbl"], item["pk"]
            at, deleted, payload = item["at"], item["deleted"], item["payload"]
            if deleted:
                encoded, enc = None, 1 if spec.private else 0
            elif spec.private:
                assert key is not None
                encoded, enc = _encrypt(key, spec.name, str(pk), payload or {}), 1
            else:
                encoded, enc = (
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    0,
                )
            statements.append(
                (
                    "INSERT INTO qf_sync_rows(tbl, pk, at, origin, deleted, enc, payload)"
                    " VALUES(?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(tbl, pk) DO UPDATE SET"
                    " at = excluded.at, origin = excluded.origin,"
                    " deleted = excluded.deleted, enc = excluded.enc,"
                    " payload = excluded.payload"
                    " WHERE excluded.at > qf_sync_rows.at",
                    (tbl, pk, at, origin, 1 if deleted else 0, enc, encoded),
                )
            )
            statements.append(
                (
                    "INSERT INTO qf_sync_log(at, origin, tbl, pk, deleted, enc, payload)"
                    " VALUES(?, ?, ?, ?, ?, ?, ?)",
                    (at, origin, tbl, pk, 1 if deleted else 0, enc, encoded),
                )
            )
            deletes.append((str(tbl), str(pk), at))
        if statements:
            remote.batch(statements)
        # 远端落了才清本地的脏标记；期间又变过（at 不同）就留着
        with engine.begin() as conn:
            for tbl, pk, at in deletes:
                conn.exec_driver_sql(
                    "DELETE FROM qf_sync_journal WHERE tbl = ? AND pk = ? AND at = ?",
                    (tbl, pk, at),
                )
        pushed += len(entries)
        if len(entries) < _PUSH_PAGE:
            break
    return {"pushed": pushed}


# ============================================================================ 应用远端行


def _apply_page(engine: Engine, rows: list[dict], key: bytes | None, origin: str) -> dict:
    """把一页远端行落到本地（一个事务；触发器静默）。"""
    applied = skipped = deleted = 0
    settings_save: dict | None = None
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA defer_foreign_keys=ON")
        conn.exec_driver_sql("INSERT OR REPLACE INTO qf_sync_state(k, v) VALUES('paused', '1')")
        attempt_rows: list[dict] = []
        message_parents: list[tuple[str, str]] = []
        attachment_messages: list[tuple[str, str]] = []

        # 这一页里会落地的对话（消息的外键要它们先存在）
        page_conversations: set[str] = set()
        for candidate in rows:
            if str(candidate.get("tbl")) != "conversations":
                continue
            if int(candidate.get("deleted") or 0):
                continue
            try:
                page_conversations.add(str(json.loads(str(candidate.get("pk")))[0]))
            except (ValueError, TypeError, IndexError):
                continue

        ordered = sorted(
            rows,
            key=lambda row: (_ORDER_INDEX.get(str(row.get("tbl")), 99), float(row.get("at") or 0)),
        )
        for remote_row in ordered:
            tbl = str(remote_row.get("tbl") or "")
            pk = str(remote_row.get("pk") or "")
            spec = SPECS.get(tbl)
            if spec is None:
                skipped += 1
                continue
            shadow = conn.exec_driver_sql(
                "SELECT at, deleted FROM qf_sync_rows WHERE tbl = ? AND pk = ?", (tbl, pk)
            ).fetchone()
            at = float(remote_row.get("at") or 0.0)
            if shadow is not None and float(shadow[0]) >= at:
                skipped += 1
                continue

            values = json.loads(pk)
            if int(remote_row.get("deleted") or 0):
                where = " AND ".join(f"{_q(col)} = ?" for col in spec.key)
                conn.exec_driver_sql(f"DELETE FROM {_q(tbl)} WHERE {where}", tuple(values))
                conn.exec_driver_sql(
                    "INSERT INTO qf_sync_rows(tbl, pk, at, deleted) VALUES(?, ?, ?, 1)"
                    " ON CONFLICT(tbl, pk) DO UPDATE SET at = excluded.at, deleted = 1",
                    (tbl, pk, at),
                )
                deleted += 1
                continue

            payload = remote_row.get("payload")
            if payload is None:
                skipped += 1
                continue
            # 以**远端这一行自己的 enc 标记**为准，而不是表当前的分类：
            # 一张表可能刚被划进 `PRIVATE_TABLES`，远端还留着它早期的明文行。
            # 那些行要能读进来（随后由 `_schedule_private_rescope` 重推成密文），
            # 否则升级当天就会因为"拿密文的口令去解明文"炸掉。
            if int(remote_row.get("enc") or 0):
                if key is None:
                    raise SyncError(
                        f"远端的 {tbl} 读不了：两端的同步凭据不是同一套"
                        "（换回原来那组地址与令牌）"
                    )
                try:
                    data = _decrypt(key, tbl, pk, str(payload))
                except (SyncError, InvalidTag, ValueError):
                    # 旧密钥留下的密文（换代 / 换令牌之后）：作废，跳过这一行。
                    # 一行坏密文不该让整次同步永远失败 —— 那正是"换完密钥就再也
                    # 同步不动"的症状。
                    skipped += 1
                    continue
            else:
                data = json.loads(str(payload))
                if not isinstance(data, dict):
                    skipped += 1
                    continue

            if spec.name == "messages":
                owner = data.get("conversation_id")
                if owner and str(owner) not in page_conversations:
                    exists_owner = conn.exec_driver_sql(
                        "SELECT 1 FROM conversations WHERE id = ?", (owner,)
                    ).fetchone()
                    if exists_owner is None:
                        # 对话还没到（或已被删）：先不落这条，影子也不推进，
                        # 等对话到了下一轮再应用。硬插会撞外键、让整页回滚。
                        skipped += 1
                        continue

            if spec.name == "app_settings":
                local = settings_file.load() or _local_settings_from_db(conn)
                merged = _merge_settings(local, data.get("data"))
                _write_settings_row(conn, merged, data.get("client_rev"))
                settings_save = merged
            elif spec.name == "records":
                _apply_record(conn, data)
            elif spec.name == "attempts":
                if _apply_attempt(conn, data):
                    attempt_rows.append(data)
            else:
                _apply_row(conn, spec, values, data, at)
                if spec.name == "messages":
                    parent_uid = data.get("parent_uid")
                    # **子消息的身份取 payload 里的 `uid`，不是 `pk`**：
                    # `pk` 是 JSON 串（`["9f3…"]`，见 `_pk_json`），拿它当 child_uid
                    # 写进 `qf_sync_parents` 就永远 JOIN 不上 `messages.uid` ——
                    # 回填 SQL 一直在空转，全量拉下来一半的消息 `parent_id` 是空的
                    # （对话树整个散掉）。
                    child_uid = str(data.get("uid") or "")
                    if parent_uid and child_uid:
                        message_parents.append((child_uid, str(parent_uid)))
                elif spec.name == "attachments":
                    message_uid = data.get("message_uid")
                    if message_uid:
                        attachment_messages.append((str(pk), str(message_uid)))

            conn.exec_driver_sql(
                "INSERT INTO qf_sync_rows(tbl, pk, at, deleted) VALUES(?, ?, ?, 0)"
                " ON CONFLICT(tbl, pk) DO UPDATE SET at = excluded.at, deleted = 0",
                (tbl, pk, at),
            )
            applied += 1

        # 消息的 parent_id / 附件的 message_id 按 uid 二次解析：
        # 同一页里父消息可能排在子消息后面，插的时候还查不到。
        # 映射**落盘**（`qf_sync_parents`）：跨页、跨轮次到的父消息也能在之后补上。
        for child_uid, parent_uid in message_parents:
            conn.exec_driver_sql(
                "INSERT INTO qf_sync_parents(child_uid, parent_uid) VALUES(?, ?)"
                " ON CONFLICT(child_uid) DO UPDATE SET parent_uid = excluded.parent_uid",
                (child_uid, parent_uid),
            )
        # **每次都补一次**，而不是"这一页里有新消息才补"：老版本拉下来的行可能
        # 一直挂着空的 `parent_id`（对话树整个散掉），而后面几轮只要还有别的表在同步，
        # 就该顺手把它们接回去 —— 这条 SQL 自己带 `parent_id IS NULL`，没有可补的就是空转。
        conn.exec_driver_sql(_BACKFILL_PARENTS_SQL)
        for attachment_pk, message_uid in attachment_messages:
            attachment_key = json.loads(attachment_pk)
            conn.exec_driver_sql(
                "UPDATE attachments SET message_id = (SELECT id FROM messages WHERE uid = ?)"
                " WHERE id = ?",
                (message_uid, attachment_key[0]),
            )

        if attempt_rows:
            session = OrmSession(bind=conn, autoflush=False, expire_on_commit=False)
            try:
                _bump_attempts(session, attempt_rows)
                session.flush()
            finally:
                session.close()

        conn.exec_driver_sql("DELETE FROM qf_sync_state WHERE k = 'paused'")

    if settings_save is not None:
        try:
            settings_file.save(settings_save)
        except OSError as exc:      # 文件写不进去不该让整页回滚（库已经落了）
            log.warning("同步来的设置没能写回文件：%s", exc)

    return {"applied": applied, "skipped": skipped, "deleted": deleted}


def _local_settings_from_db(conn) -> dict:  # noqa: ANN001
    row = conn.exec_driver_sql("SELECT data FROM app_settings WHERE id = 1").fetchone()
    if row is None:
        return {}
    try:
        data = json.loads(row[0]) if isinstance(row[0], (str, bytes)) else row[0]
    except (ValueError, TypeError):
        return {}
    return normalize(data) if isinstance(data, dict) else {}


def _write_settings_row(conn, data: dict, client_rev: Any) -> None:  # noqa: ANN001
    raw = json.dumps(data, ensure_ascii=False)
    exists = conn.exec_driver_sql("SELECT 1 FROM app_settings WHERE id = 1").fetchone()
    if exists is None:
        conn.exec_driver_sql(
            "INSERT INTO app_settings(id, data, client_rev, updated_at) VALUES(1, ?, ?, datetime('now'))",
            (raw, int(client_rev or 0)),
        )
    else:
        conn.exec_driver_sql(
            "UPDATE app_settings SET data = ?, client_rev = ?, updated_at = datetime('now') WHERE id = 1",
            (raw, int(client_rev or 0)),
        )


def _apply_row(conn, spec: TableSpec, values: list[Any], data: dict, at: float) -> None:  # noqa: ANN001
    table = _table(spec.name)
    columns = [col.name for col in table.columns]
    row_values: dict[str, Any] = {}
    for name in columns:
        if name in spec.generated:
            continue
        if name in data:
            row_values[name] = _from_json_value(data[name])
    if spec.name == "messages":
        # **远端"没有父"不等于要把本地的父擦掉。**
        # 2026-11-09 实测：一台本地 `parent_id` 全空的机器整表重推（日志 seq
        # 3468–4084），把远端 617 条已经带父的消息洗成了 `parent_uid = null`；
        # 那台机器再拉回来，`_apply_row` 又用 null 覆盖自己（本来就空），于是
        # 两端一起断链，且越拉越"确认"这个错误状态。
        # 所以只在**解析得到**父行时才写 `parent_id`：载荷里 `parent_uid` 为空
        # （键缺失或显式 null）都当"这一行没提供父信息"，保留本机已有的指针。
        # 代价是"把一条消息明确改成根"不会随同步生效 —— 应用里没有这种操作
        # （编辑重发是新增一条、带 `parentId`），换来的是一棵树不会被空载荷洗掉。
        parent_uid = data.get("parent_uid")
        parent_id = _local_id_for_uid(conn, "messages", parent_uid)
        if parent_id is not None:
            row_values["parent_id"] = parent_id
        else:
            row_values.pop("parent_id", None)
    elif spec.name == "attachments":
        message_uid = data.get("message_uid")
        row_values["message_id"] = _local_id_for_uid(conn, "messages", message_uid)

    where = " AND ".join(f"{_q(col)} = ?" for col in spec.key)
    exists = conn.exec_driver_sql(
        f"SELECT 1 FROM {_q(spec.name)} WHERE {where}", tuple(values)
    ).fetchone()
    if exists is not None:
        updates = {k: v for k, v in row_values.items() if k not in spec.key}
        if updates:
            assignments = ", ".join(f"{_q(col)} = ?" for col in updates)
            conn.exec_driver_sql(
                f"UPDATE {_q(spec.name)} SET {assignments} WHERE {where}",
                (*updates.values(), *values),
            )
        return

    if spec.name == "conversation_folders" and row_values.get("path"):
        # 分组在**本机**按 path 唯一（`UNIQUE(path)`），但同步身份是 uid ——
        # 两台机器各建了一个同名分组时，两边 uid 不同，按 uid 查不到就会 INSERT，
        # 然后撞 path 唯一约束（实测：`UNIQUE constraint failed: conversation_folders.path`）。
        # 分组身份本来就是路径（`Conversation.folder` 存的就是路径字符串，不引用 uid），
        # 所以这里认成同一个：把本机那行的 uid 对齐成远端这份，跳过插入。
        seen = conn.exec_driver_sql(
            "SELECT uid FROM conversation_folders WHERE path = ?", (row_values["path"],)
        ).fetchone()
        if seen is not None and str(seen[0]) != str(row_values.get("uid") or ""):
            conn.exec_driver_sql(
                "UPDATE conversation_folders SET uid = ? WHERE uid = ?",
                (row_values["uid"], seen[0]),
            )
            return

    # **老行推上来时可能还没有这一列**（payload 是稀疏的）。新库是 `create_all`
    # 建出来的，模型上的客户端默认值**不会变成 DDL 里的 DEFAULT**，于是那条裸
    # INSERT 会撞 `NOT NULL constraint failed: conversations.title_auto`（实测：
    # 手机首次全量拉取就死在这张表上，一条都拉不下来）。这里按模型上的标量默认值
    # 补齐 —— 与 `db._ensure_columns` 给老库补列时用的是同一档默认值。
    for column in table.columns:
        if column.name in row_values or column.name in spec.generated:
            continue
        if column.nullable or column.server_default is not None:
            continue
        default = column.default
        if default is None:
            continue
        if default.is_scalar:
            row_values[column.name] = default.arg
        elif callable(default.arg):
            # `default=dict` / `list` 这种：没有 context 也能调
            row_values[column.name] = default.arg()

    names = ", ".join(_q(col) for col in row_values)
    marks = ", ".join("?" for _ in row_values)
    conn.exec_driver_sql(
        f"INSERT INTO {_q(spec.name)} ({names}) VALUES ({marks})",
        tuple(row_values.values()),
    )


_RECORD_DEFAULTS: dict[str, Any] = {
    "attempts": 0,
    "correct": 0,
    "partial": 0,
    "wrong": 0,
    "first_at": 0,
    "last_at": 0,
    "last_status": "",
    "last_score": 0.0,
    "last_response": None,
    "patch": "{}",
    "mastered": 0,
    "flagged": 0,
    "client_rev": 0,
}


def _apply_record(conn, data: dict) -> None:  # noqa: ANN001
    """records：主观字段按 LWW，计数列**不覆盖**（由流水维护）。"""
    question_id = str(data.get("question_id") or "")
    if not question_id:
        return
    exists = conn.exec_driver_sql(
        "SELECT 1 FROM records WHERE question_id = ?", (question_id,)
    ).fetchone()
    subjective = {
        key: _from_json_value(data.get(key, _RECORD_DEFAULTS[key]))
        for key in ("patch", "mastered", "flagged", "client_rev")
    }
    if exists is not None:
        conn.exec_driver_sql(
            "UPDATE records SET patch = ?, mastered = ?, flagged = ?, client_rev = ?"
            " WHERE question_id = ?",
            (*subjective.values(), question_id),
        )
        return
    # 本机还没有这行：整行插入，但计数列只取**流水表示不了的那部分差额**
    # （`sync_delta`，见上传侧）。远端计数已经包含那边的流水；这些流水
    # 随后也会被拉下来并累加，照抄计数再加流水就是两遍（新机器实测：
    # 1 条流水记成 2 次）。而纯差额（导入的旧进度没有流水）必须留着。
    # 首次/最近时间与最近判定保留：它们不随流水重复累加（SQL 里是 min/max）。
    row = dict(_RECORD_DEFAULTS)
    for column in _table("records").columns:
        name = column.name
        if name == "question_id":
            continue
        if name in data:
            row[name] = _from_json_value(data[name])
    delta = _json_dict(data.get("sync_delta"))
    for counter in ("attempts", "correct", "partial", "wrong"):
        row[counter] = max(0, int(delta.get(counter) or 0))
    row["question_id"] = question_id
    names = ", ".join(_q(col) for col in row)
    marks = ", ".join("?" for _ in row)
    conn.exec_driver_sql(f"INSERT INTO records ({names}) VALUES ({marks})", tuple(row.values()))


def _apply_attempt(conn, data: dict) -> bool:  # noqa: ANN001
    """插一条流水；返回**是否真的插入了**（已存在 = False，不重复计数）。"""
    columns = [col.name for col in _table("attempts").columns if col.name in data]
    if "id" not in columns:
        return False
    names = ", ".join(_q(col) for col in columns)
    marks = ", ".join("?" for _ in columns)
    cursor = conn.exec_driver_sql(
        f"INSERT INTO attempts ({names}) VALUES ({marks}) ON CONFLICT(id) DO NOTHING",
        tuple(_from_json_value(data[col]) for col in columns),
    )
    return bool(cursor.rowcount)


def _bump_attempts(session: OrmSession, rows: list[dict]) -> None:
    """把刚拉进来的流水累加进 records / days（复用进度同步那套口径）。"""
    from .sync_ops import bump_days, bump_records  # noqa: PLC0415

    parsed = []
    for row in rows:
        at = _parse_dt(row.get("at"))
        day = _parse_day(row.get("day"))
        if at is None or day is None:
            continue
        parsed.append(
            {
                "id": row.get("id"),
                "questionId": str(row.get("question_id") or ""),
                "atMs": int(at.timestamp() * 1000),
                "day": day,
                "status": str(row.get("status") or ""),
                "score": float(row.get("score") or 0.0),
                "response": _from_json_value(row.get("response")),
                "topicKey": str(row.get("topic_key") or ""),
            }
        )
    bump_records(session, parsed)
    bump_days(session, parsed)


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    raw = str(value).replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _parse_day(value: Any) -> date_type | None:
    if value is None:
        return None
    if isinstance(value, date_type) and not isinstance(value, datetime):
        return value
    try:
        return date_type.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _from_json_value(value: Any) -> Any:
    """序列化时的二进制标记（目前用户表里没有 BLOB，留着给以后）。"""
    if isinstance(value, dict) and set(value) == {"$b64"}:
        return base64.b64decode(str(value["$b64"]))
    return value


# ============================================================================ 拉取


def _heal_parent_mapping(engine: Engine) -> bool:
    """检测**写歪或缺失的父子映射**，有就清空并返回 True（重建见 `_repair_parent_mapping`）。

    两种形态：

    * 老版本把 `child_uid` 落成了主键的 JSON 串（`["9f3…"]`），与 `messages.uid`
      永远对不上，回填 SQL 一直在空转 —— 表现是"全量拉完，一半消息的父节点没了"；
    * 更老的版本里还没有这张表，映射**压根没建过**。判据里要带上"拉取游标已经在、
      并且本机同步过消息"：从没拉过的机器（只推不拉的 origin）不该被拽去重建。
    """
    with engine.begin() as conn:
        broken = conn.exec_driver_sql(
            "SELECT 1 FROM qf_sync_parents WHERE child_uid LIKE '[%' LIMIT 1"
        ).fetchone()
        empty = broken is None and conn.exec_driver_sql(
            "SELECT 1 FROM qf_sync_parents LIMIT 1"
        ).fetchone() is None
        if empty and _state_get(conn, "last_pull_seq") is None:
            empty = False
        if empty and conn.exec_driver_sql(
            "SELECT 1 FROM qf_sync_rows WHERE tbl = 'messages' LIMIT 1"
        ).fetchone() is None:
            empty = False
        if broken is None and not empty:
            return False
        conn.exec_driver_sql("DELETE FROM qf_sync_parents")
    return True


def _parent_pairs_from_log(remote: Any, key: bytes | None) -> dict[str, str]:
    """从 `qf_sync_log` 的历史里取每个消息 uid **最后一次非空** 的 `parent_uid`。

    为什么需要它：镜像（`qf_sync_rows`）只保留**当前值**，一次空载荷就能把父指针
    从镜像里彻底抹掉（2026-11-09 就是这么丢了 617 条）。日志是追加的，历史还在；
    这是断链之后**唯一**的恢复来源 —— 只要那条消息曾经带过父，就能找回来。
    """
    pairs: dict[str, str] = {}
    cursor = 0
    while True:
        rows = remote.query(
            "SELECT seq, pk, enc, payload FROM qf_sync_log"
            " WHERE tbl = 'messages' AND seq > ? ORDER BY seq LIMIT ?",
            (cursor, _PULL_PAGE),
        )
        if not rows:
            break
        for row in rows:
            cursor = int(row["seq"])
            if row.get("payload") is None:
                continue
            try:
                if int(row.get("enc") or 0):
                    if key is None:
                        continue
                    data = _decrypt(key, "messages", str(row["pk"]), str(row["payload"]))
                else:
                    data = json.loads(str(row["payload"]))
            except (SyncError, ValueError, TypeError):
                continue
            child = str(data.get("uid") or "")
            parent = data.get("parent_uid")
            # 按 seq 递增遍历：后面的覆盖前面的；空值**不覆盖**（保留最后一次非空）
            if child and parent:
                pairs[child] = str(parent)
        if len(rows) < _PULL_PAGE:
            break
    return pairs


def _repair_parent_mapping(engine: Engine, remote: Any, key: bytes | None) -> int:
    """重建 `qf_sync_parents`，再把 `parent_id` 接回去。

    两个来源，**镜像优先、日志兜底**：
    * 镜像 `qf_sync_rows` 是"当前值"（最新事实）；
    * 日志 `qf_sync_log` 是历史，镜像里被洗空的那些只能靠它（见
      `_parent_pairs_from_log`）。空值一律不当事实 —— 与 `_apply_row` 同一口径：
      **父指针只会被补上，不会被空载荷擦掉**。

    为什么不能靠"再全量拉一遍"：拉取有一条影子时间戳的短路（`qf_sync_rows.at`
    不比远端旧就跳过），全量重放时每条都被判成"已是最新"，一条也不会重新落库
    （实测：清掉游标再拉，2050 行全被 skip，父节点一个都没接回来）。

    这么修也**不碰正文**：只读 `uid` / `parent_uid` 两列，写进映射表，再跑一次回填。
    """
    if remote is None:
        return 0
    mirror: dict[str, str] = {}
    last = ""
    while True:
        rows = remote.query(
            "SELECT pk, enc, payload, deleted FROM qf_sync_rows"
            " WHERE tbl = 'messages' AND pk > ? ORDER BY pk LIMIT ?",
            (last, _FULL_PAGE),
        )
        if not rows:
            break
        for row in rows:
            last = str(row.get("pk") or "")
            if int(row.get("deleted") or 0) or row.get("payload") is None:
                continue
            try:
                if int(row.get("enc") or 0):
                    if key is None:
                        continue
                    data = _decrypt(key, "messages", last, str(row["payload"]))
                else:
                    data = json.loads(str(row["payload"]))
            except (SyncError, ValueError, TypeError):
                continue
            child = str(data.get("uid") or "")
            parent = data.get("parent_uid")
            if child and parent:
                mirror[child] = str(parent)
        if len(rows) < _FULL_PAGE:
            break
    pairs = _parent_pairs_from_log(remote, key)
    pairs.update(mirror)          # 镜像是最新事实，同一条以它为准
    if not pairs:
        return 0
    with engine.begin() as conn:
        for child, parent in pairs.items():
            conn.exec_driver_sql(
                "INSERT INTO qf_sync_parents(child_uid, parent_uid) VALUES(?, ?)"
                " ON CONFLICT(child_uid) DO UPDATE SET parent_uid = excluded.parent_uid",
                (child, parent),
            )
        conn.exec_driver_sql(_BACKFILL_PARENTS_SQL)
    return len(pairs)


def _pull(engine: Engine, remote: Any, key: bytes | None, origin: str) -> dict:
    pulled = applied = deletes = 0
    if _heal_parent_mapping(engine):
        try:
            _repair_parent_mapping(engine, remote, key)
        except Exception:  # noqa: BLE001 - 修不回来不该拖垮这一轮同步
            log.warning("父子映射重建失败（下一轮再试）", exc_info=True)
    with engine.begin() as conn:
        cursor_raw = _state_get(conn, "last_pull_seq")
    if cursor_raw is None:
        return _full_pull(engine, remote, key, origin)

    cursor = int(cursor_raw)
    while True:
        rows = remote.query(
            "SELECT seq, at, origin, tbl, pk, deleted, enc, payload"
            " FROM qf_sync_log WHERE seq > ? ORDER BY seq LIMIT ?",
            (cursor, _PULL_PAGE),
        )
        if not rows:
            break
        page = _apply_page(engine, rows, key, origin)
        cursor = max(int(row["seq"]) for row in rows)
        with engine.begin() as conn:
            _state_set(conn, "last_pull_seq", cursor)
        pulled += len(rows)
        applied += page["applied"]
        deletes += page["deleted"]
        if len(rows) < _PULL_PAGE:
            break
    return {"pulled": pulled, "applied": applied, "deleted": deletes}


def _full_pull(engine: Engine, remote: Any, key: bytes | None, origin: str) -> dict:
    """首次同步：按依赖顺序逐表读远端镜像。

    游标取**开始之前**的日志最大 seq：期间别人推到的新日志留给下一次增量，
    不会漏（写进 meta 的删除/新增都排在它后面）。
    """
    with engine.begin() as conn:
        stored = _state_get(conn, "full_pull_cursor")
        cursors: dict[str, str] = json.loads(stored) if stored else {}
        started_raw = _state_get(conn, "full_pull_seq")
    if started_raw is None:
        # **第一次**进全量拉取时记下日志上界。中途断了再进来必须沿用同一个值：
        # 重算会把断点期间别人推的新日志算进"已包含在镜像里"，
        # 而那条镜像行可能已经翻过去了 —— 那段改动就永远丢了。
        started = int(
            remote.query("SELECT COALESCE(MAX(seq), 0) AS m FROM qf_sync_log")[0]["m"] or 0
        )
        with engine.begin() as conn:
            _state_set(conn, "full_pull_seq", started)
    else:
        started = int(started_raw)
    pulled = applied = deletes = 0
    for name in PULL_ORDER:
        last_pk = cursors.get(name, "")
        while True:
            rows = remote.query(
                "SELECT tbl, pk, at, origin, deleted, enc, payload"
                " FROM qf_sync_rows WHERE tbl = ? AND pk > ? ORDER BY pk LIMIT ?",
                (name, last_pk, _FULL_PAGE),
            )
            if not rows:
                break
            page = _apply_page(engine, rows, key, origin)
            last_pk = str(rows[-1]["pk"])
            with engine.begin() as conn:
                cursors[name] = last_pk
                _state_set(conn, "full_pull_cursor", json.dumps(cursors, ensure_ascii=False))
            pulled += len(rows)
            applied += page["applied"]
            deletes += page["deleted"]
            if len(rows) < _FULL_PAGE:
                break
        if name not in cursors:
            cursors[name] = ""
    with engine.begin() as conn:
        # 收尾再补一次父子：父消息可能正好落在**最后一页**，而那一页末尾的回填跑在
        # 它插入之前（页内顺序按 uid 排，不保证父在前）—— 不补这一次，最末几层的
        # `parent_id` 会一直是空的（对话树散掉的另一种成因）。
        conn.exec_driver_sql(_BACKFILL_PARENTS_SQL)
        _state_set(conn, "last_pull_seq", started)
        _state_del(conn, "full_pull_cursor")
        _state_del(conn, "full_pull_seq")
    return {"pulled": pulled, "applied": applied, "deleted": deletes}


# ============================================================================ 一次同步


def _origin(engine: Engine) -> str:
    with engine.begin() as conn:
        origin = _state_get(conn, "origin")
        if not origin:
            origin = secrets.token_hex(8)
            _state_set(conn, "origin", origin)
        return origin


def _private_needed(engine: Engine) -> bool:
    with engine.connect() as conn:
        for name in PRIVATE_TABLES:
            if conn.exec_driver_sql(f"SELECT 1 FROM {_q(name)} LIMIT 1").fetchone() is not None:
                return True
        row = conn.exec_driver_sql(
            "SELECT 1 FROM qf_sync_journal WHERE tbl IN ("
            + ", ".join("?" for _ in PRIVATE_TABLES)
            + ") LIMIT 1",
            tuple(PRIVATE_TABLES),
        ).fetchone()
        return row is not None


def _remote_has_private(remote: Any) -> bool:
    rows = remote.query("SELECT 1 AS x FROM qf_sync_rows WHERE enc = 1 LIMIT 1")
    return bool(rows)


def run_once(engine: Engine | None = None, remote: Any | None = None) -> dict:
    """推一次、拉一次。返回这次的统计。"""
    global _running, _last_error
    engine = engine or get_engine()
    ensure_sync_schema(engine)
    if not turso_enabled():
        raise SyncNotConfigured("同步已关闭（设置里的 turso.enabled = false）")
    conf = turso_config()
    if not turso_configured():
        raise SyncNotConfigured(
            "还没配 Turso：在设置 →「同步」里填数据库地址与令牌，"
            "或直接改 config/settings.local.json 的 turso 块。"
        )
    origin = _origin(engine)
    started = time.monotonic()
    try:
        # **构造也必须在 try 里**（2026-11-11）：它要读地址与令牌，配置半坏时会在这
        # 一步就抛。放在外面的话异常不落库，设置页读到的还是上一次的「推 0 / 拉 0」，
        # 用户看到的是"同步完成"。
        remote = remote or TursoClient(str(conf["url"]), str(conf["token"]))
        remote.ensure_schema()
        need_private = _private_needed(engine) or _remote_has_private(remote)
        key = _remote_key(remote, need_private=need_private)
        if key is not None:
            migrated = _migrate_key(engine, remote, key)
            if migrated:
                log.info("密钥换代：%d 行加密数据待重推（旧密文作废）", migrated)
        baseline = _baseline(engine)
        pushed = _push(engine, remote, key, origin)
        pulled = _pull(engine, remote, key, origin)
        # 新划进加密范围的表：**拉完之后**再排重推。顺序很重要 ——
        # 先拉才能拿到另一台机器的最新行；先推会让本机的旧副本借新的
        # 时间戳盖掉那边刚写的计划书。排进队列的行由**下一次**推送换成密文。
        _schedule_private_rescope(engine)
        # 父指针最后再补一次：跨页/跨轮次才到的父消息在这里接上（页内已经补过一次）
        with engine.begin() as conn:
            conn.exec_driver_sql(_BACKFILL_PARENTS_SQL)
        result = {
            "ok": True,
            "at": _now_iso(),
            "seconds": round(time.monotonic() - started, 2),
            "baseline": baseline,
            **pushed,
            **pulled,
            "origin": origin,
        }
    except Exception as exc:
        _last_error = _error_text(exc)
        _record_failure(engine, exc)
        raise
    with engine.begin() as conn:
        _state_set(conn, "last_result", json.dumps(result, ensure_ascii=False))
        _state_set(conn, "last_sync_at", result["at"])
    return result


def run_full(engine: Engine | None = None, remote: Any | None = None) -> dict:
    """一次**完整**同步：行级表 + 资料整包 + 题库整包。

    三套搬运方式（行级 / 资料包 / 题库包）对用户来说是同一件事：所有数据。
    手动按钮、`make sync`、后台节拍走的都是这一个入口。

    **顺序有讲究**：题库包里的点靠 `materials.slug` 落地，所以资料包必须先跑完
    （否则那一轮只能拉到题目，知识点要等下一轮）。

    两个整包失败都不回滚行级，也不把整次同步判死：结果里分别给 `library` /
    `bank`，失败时带 `error`。行级失败仍然抛（那是更基础的一层）。
    """
    result = run_once(engine, remote)
    try:
        from . import library_cloud  # noqa: PLC0415 - library_cloud 反过来 import sync

        with get_session_factory()() as db:
            result["library"] = library_cloud.sync_pack(db)
    except Exception as exc:  # noqa: BLE001 - 资料包失败只记账
        log.warning("资料包同步失败：%s", exc)
        result["library"] = {"ok": False, "error": str(exc)}

    try:
        from . import bank_cloud  # noqa: PLC0415 - 同上

        with get_session_factory()() as db:
            result["bank"] = bank_cloud.sync_pack(db)
    except Exception as exc:  # noqa: BLE001 - 题库包失败只记账
        log.warning("题库包同步失败：%s", exc)
        result["bank"] = {"ok": False, "error": str(exc)}

    # 把含资料包的结果写回状态（`run_once` 只知道行级那一半）。
    try:
        with (engine or get_engine()).begin() as conn:
            _state_set(conn, "last_result", json.dumps(result, ensure_ascii=False))
    except Exception:  # noqa: BLE001 - 状态写不进去不影响同步本身
        log.warning("同步结果没能写进状态表", exc_info=True)
    return result


def run_in_background(engine: Engine | None = None) -> bool:
    """起一个后台线程跑一次；已经在跑就返回 False。"""
    global _running
    with _run_lock:
        if _running:
            return False
        _running = True

    def worker() -> None:
        global _running
        try:
            run_full(engine)
        except Exception as exc:  # noqa: BLE001 - 后台线程里吞掉，状态里有
            log.warning("后台同步失败：%s", _error_text(exc))
            # 兜底再写一次：`run_once` 的 except 已经写过，但**它 try 之外**那几行
            # （读配置、建客户端）抛出来的异常到不了那里 —— 那种失败必须也留痕，
            # 否则设置页会继续显示上一次的「推 0 / 拉 0」（2026-11-11 用户报障）。
            _record_failure(engine or get_engine(), exc)
        finally:
            with _run_lock:
                _running = False

    threading.Thread(target=worker, name="qf-sync", daemon=True).start()
    return True


def is_running() -> bool:
    return _running


def remote_changed(engine: Engine | None = None, remote: Any | None = None) -> dict:
    """远端有没有本机还没拉到的行 —— 一次单行查询，用来决定"要不要现在跑一轮"。

    只看 `qf_sync_log` 的 `MAX(seq)`：别人写进来的东西都从那里进来。自己推上去的行
    会在同一轮的 `_pull` 里把游标推到同一位置，所以不会在这里反复命中自己。
    没配 / 没启用时返回 `changed=False`，自动同步那侧就只按本地改动走。
    """
    if not (turso_enabled() and turso_configured()):
        return {"changed": False, "reason": "同步没开或没配"}
    engine = engine or get_engine()
    ensure_sync_schema(engine)
    with engine.connect() as conn:
        cursor_raw = _state_get(conn, "last_pull_seq")
    if cursor_raw is None:
        # 第一次（或全量拉取中断）—— 游标还没落，必须让它跑。
        return {"changed": True, "reason": "还没拉过"}
    conf = turso_config()
    if remote is None:
        remote = TursoClient(str(conf["url"]), str(conf["token"]))
    rows = remote.query("SELECT COALESCE(MAX(seq), 0) AS m FROM qf_sync_log")
    head = int(rows[0]["m"] or 0) if rows else 0
    cursor = int(cursor_raw)
    return {"changed": head > cursor, "cursor": cursor, "head": head, "reason": ""}


# ============================================================================ 状态


def status(engine: Engine | None = None) -> dict:
    """给设置页看的一份状态。**绝不返回 token。**

    只读：设置页每 2 秒轮询一次，**不能**顺手建表/回填 —— 那是一个写事务，
    会和后台同步抢锁（2026-11-10 的 `database is locked` 就是这么来的）。
    表还没建时（全新库）才补一次。
    """
    engine = engine or get_engine()
    with engine.connect() as conn:
        ready = _has_table(conn, "qf_sync_journal")
    if not ready:
        ensure_sync_schema(engine)
    conf = turso_config()
    with engine.connect() as conn:
        pending = conn.exec_driver_sql("SELECT COUNT(*) FROM qf_sync_journal").fetchone()[0]
        origin = _state_get(conn, "origin")
        cursor = _state_get(conn, "last_pull_seq")
        last = _state_get(conn, "last_result")
        baseline = _state_get(conn, "baseline_done") == "1"
        counts = {
            name: conn.exec_driver_sql(f"SELECT COUNT(*) FROM {_q(name)}").fetchone()[0]
            for name in SYNC_TABLES
        }
    try:
        last_result = json.loads(last) if last else None
    except ValueError:
        last_result = None
    return {
        "configured": turso_configured(),
        "enabled": turso_enabled(),
        "url": str(conf.get("url") or ""),
        "running": _running,
        "origin": origin or "",
        "pending": int(pending),
        "cursor": int(cursor) if cursor is not None else None,
        "baselineDone": baseline,
        "last": last_result,
        "keyReady": bool(_passphrase()),
        "keyFile": str(settings_file.path()),
        "privateTables": sorted(PRIVATE_TABLES),
        "tables": counts,
        "lastError": _last_error,
    }


__all__ = [
    "PRIVATE_TABLES",
    "SPECS",
    "SYNC_TABLES",
    "SyncError",
    "SyncNotConfigured",
    "TursoClient",
    "ensure_sync_schema",
    "is_running",
    "remote_changed",
    "run_full",
    "run_in_background",
    "run_once",
    "status",
    "turso_configured",
    "turso_config",
    "turso_enabled",
]
