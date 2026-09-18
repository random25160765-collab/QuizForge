"""概念的「以概念为中心的判边」问过没有：concepts.centric_at

配对判边把现网共现对判完之后，产出很低（1912 对 → 19 条边）—— 「同切片共现」
这个信号本身撑不起"前置"。所以再开一条路：**给模型一个概念 + 它的若干候选，
问"要读懂它得先懂哪几个"**（见 `pipeline.graph_build relate-centric`）。

这一列只为一件事：**别把同一个概念反复问**。模型说"没有前置"的也要留痕，
不然下次查询照样选中它，钱白花。

Revision ID: b8d52e0f1a44
Revises: a7c41d9e2b30
Create Date: 2026-09-18 14:00:00.000000+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b8d52e0f1a44'
down_revision: Union[str, Sequence[str], None] = 'a7c41d9e2b30'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'concepts',
        sa.Column('centric_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('concepts', 'centric_at')
