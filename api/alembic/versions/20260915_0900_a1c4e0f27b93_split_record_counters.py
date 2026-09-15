"""records 拆分「服务端计数列」与「客户端补丁列」；attempts 增加本地日期

## 为什么必须写成 ADD COLUMN + 回填，而不是重建表

前一条迁移（`20bd6c290c23`）在表里没有真实数据时用了 drop/create。
这一次 `records` 里已经有用户的真实计数 —— 重建会让所有人的进度归零。
所以这里全部是 `ADD COLUMN`，新列的值从**现有的 `payload` JSON 回填**，
最后才把 `payload` 删掉（它的内容已被拆到各列，不再有独立信息）。

## 拆分口径

* 计数列（attempts/correct/partial/wrong/first_at/last_at/last_status/
  last_score/last_response）：今后只由 attempts 流水增量维护，不再接受客户端上传
* `patch` 列（sm2 / note / streak）：客户端主观状态，按 client_rev 做 LWW
* mastered / flagged：已是独立布尔列，保持不变

Revision ID: a1c4e0f27b93
Revises: fce2d43e76ba
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a1c4e0f27b93"
down_revision = "fce2d43e76ba"
branch_labels = None
depends_on = None

# 数字回填的安全转换：payload 是客户端写的 JSON，字段可能缺失或不是数字
_INT = "(CASE WHEN payload->>'{key}' ~ '^-?[0-9]+$' THEN (payload->>'{key}')::bigint ELSE 0 END)"
_FLOAT = (
    "(CASE WHEN payload->>'{key}' ~ '^-?[0-9]+(\\.[0-9]+)?$' "
    "THEN (payload->>'{key}')::double precision ELSE 0 END)"
)


def upgrade() -> None:
    # ---------------------------------------------------------- attempts.day
    # 新增「归属哪一天」。历史行没有这个信息，只能按 at 的 UTC 日期尽力回填 ——
    # 这是本次改造唯一一处有损的地方，且只影响改造前的老流水。
    op.add_column("attempts", sa.Column("day", sa.Date(), nullable=True))
    op.execute("UPDATE attempts SET day = (at AT TIME ZONE 'UTC')::date WHERE day IS NULL")
    op.alter_column("attempts", "day", nullable=False)
    op.create_index("ix_attempts_day", "attempts", ["day"])

    # ------------------------------------------------------- records 新列
    op.add_column("records", sa.Column("partial", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("records", sa.Column("first_at", sa.BigInteger(), nullable=False, server_default="0"))
    op.add_column("records", sa.Column("last_status", sa.String(length=16), nullable=False, server_default=""))
    op.add_column("records", sa.Column("last_score", sa.Float(), nullable=False, server_default="0"))
    op.add_column(
        "records",
        sa.Column("last_response", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "records",
        sa.Column(
            "patch",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )

    # ------------------------------------------------- 从 payload 回填新列
    op.execute(
        f"""
        UPDATE records SET
            partial       = {_INT.format(key="partial")},
            first_at      = {_INT.format(key="firstAt")},
            last_status   = COALESCE(payload->>'lastStatus', ''),
            last_score    = {_FLOAT.format(key="lastScore")},
            last_response = payload->'lastResponse'
        """
    )

    # 补丁列：把 sm2 / note / streak 归拢进一个 JSON。
    # 只挑这三个键，避免把已经提升为列的字段（attempts 等）又复制一份 ——
    # 两份状态迟早会不一致。
    op.execute(
        f"""
        UPDATE records SET patch = jsonb_build_object(
            'sm2',    payload->'sm2',
            'note',   COALESCE(payload->>'note', ''),
            'streak', {_INT.format(key="streak")}
        )
        """
    )

    # payload 的内容已全部拆到列上，保留它只会形成第二份真相
    op.drop_column("records", "payload")

    # 回填完成，去掉临时默认值，让应用层成为唯一的值来源
    for column in ("partial", "first_at", "last_status", "last_score", "patch"):
        op.alter_column("records", column, server_default=None)


def downgrade() -> None:
    """回退到「整份 payload」形状。

    计数与补丁可以拼回 payload，但**流水（attempts）不删** ——
    它是独立的事实，回退这次迁移不该抹掉历史。
    """
    op.add_column(
        "records",
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.execute(
        """
        UPDATE records SET payload = jsonb_build_object(
            'id',           question_id,
            'attempts',     attempts,
            'correct',      correct,
            'partial',      partial,
            'wrong',        wrong,
            'lastAt',       last_at,
            'lastStatus',   last_status,
            'lastScore',    last_score,
            'lastResponse', last_response,
            'firstAt',      first_at,
            'sm2',          patch->'sm2',
            'note',         COALESCE(patch->>'note', ''),
            'streak',       COALESCE((patch->>'streak')::int, 0),
            'flagged',      flagged,
            'mastered',     mastered,
            '_rev',         client_rev
        )
        """
    )
    op.alter_column("records", "payload", server_default=None)

    for column in ("patch", "last_response", "last_score", "last_status", "first_at", "partial"):
        op.drop_column("records", column)

    op.drop_index("ix_attempts_day", table_name="attempts")
    op.drop_column("attempts", "day")
