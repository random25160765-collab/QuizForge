"""用户题单：user_questions

模型自己出的题落在**另一张表**里，与公共题库分开 —— 后者的统计口径
（覆盖率对账、图谱的 question_concepts、出题流水线的状态机）不该被用户随手出的题影响，
而且用户题要能删（公共表的题只下架）。

payload 与 `questions.payload` 同构，所以渲染/判分/练习/组卷那些代码不必分叉。

Revision ID: a7c41d9e2b30
Revises: f5eecfb7c30c
Create Date: 2026-09-17 15:00:00.000000+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7c41d9e2b30'
down_revision: Union[str, Sequence[str], None] = 'f5eecfb7c30c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'user_questions',
        sa.Column('id', sa.String(length=96), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('conversation_id', sa.Uuid(), nullable=True),
        sa.Column('point_key', sa.String(length=96), server_default='', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_user_questions_user_id', 'user_questions', ['user_id'])
    op.create_index(
        'ix_user_questions_user_created', 'user_questions', ['user_id', 'created_at']
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_user_questions_user_created', table_name='user_questions')
    op.drop_index('ix_user_questions_user_id', table_name='user_questions')
    op.drop_table('user_questions')
