"""records payload and sync rev

把 records 表从「逐字段建列」改成「payload JSONB + 少量提升列」。

**直接重建表，不做数据迁移**：`last_at` 从 timestamptz 改成 epoch 毫秒的
BIGINT，Postgres 无法隐式转换；而且此刻产品尚未上线，records 里没有真实数据。
如果这份迁移要跑在已有用户数据的库上，必须先写一个把旧列搬进 payload 的
数据迁移，不能直接用这一版。

Revision ID: 20bd6c290c23
Revises: f73a80b1a9b9
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20bd6c290c23"
down_revision = "f73a80b1a9b9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_records_user_updated", table_name="records")
    op.drop_index("ix_records_last_at", table_name="records")
    op.drop_index("ix_records_flagged", table_name="records")
    op.drop_table("records")

    op.create_table(
        "records",
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("question_id", sa.String(length=96), nullable=False),
        # 前端记录对象的原样 JSON —— 权威形状由前端定义，服务端不再逐字段映射
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        # 提升列：只放需要被查询/聚合的
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("correct", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("wrong", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("mastered", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("flagged", sa.Boolean(), nullable=False, server_default=sa.false()),
        # epoch 毫秒，避免时区来回转换
        sa.Column("last_at", sa.BigInteger(), nullable=False, server_default="0"),
        # 客户端单调递增序号：拒绝更旧的写入
        sa.Column("client_rev", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "question_id"),
    )
    op.create_index("ix_records_flagged", "records", ["flagged"])
    op.create_index("ix_records_last_at", "records", ["last_at"])
    op.create_index("ix_records_user_updated", "records", ["user_id", "updated_at"])


def downgrade() -> None:
    op.drop_index("ix_records_user_updated", table_name="records")
    op.drop_index("ix_records_last_at", table_name="records")
    op.drop_index("ix_records_flagged", table_name="records")
    op.drop_table("records")

    # 回到逐字段建列的旧形状（同样不保留数据）
    op.create_table(
        "records",
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("question_id", sa.String(length=96), nullable=False),
        sa.Column("first_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("correct", sa.Integer(), nullable=False),
        sa.Column("partial", sa.Integer(), nullable=False),
        sa.Column("wrong", sa.Integer(), nullable=False),
        sa.Column("streak", sa.Integer(), nullable=False),
        sa.Column("last_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status", sa.String(length=16), nullable=False),
        sa.Column("last_score", sa.Float(), nullable=False),
        sa.Column("last_response", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("mastered", sa.Boolean(), nullable=False),
        sa.Column("flagged", sa.Boolean(), nullable=False),
        sa.Column("sm2", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("self_grades", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("client_rev", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "question_id"),
    )
    op.create_index("ix_records_flagged", "records", ["flagged"])
    op.create_index("ix_records_last_at", "records", ["last_at"])
    op.create_index("ix_records_user_updated", "records", ["user_id", "updated_at"])
