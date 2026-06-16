"""add roast_conversations table

Revision ID: a1b2c3d4e5f6
Revises: 56cca7669edf
Create Date: 2026-06-16 14:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = '56cca7669edf'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'roast_conversations',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.Text(), nullable=False),
        sa.Column('roast_id', sa.Text(), nullable=False),
        sa.Column('roast_instance_id', sa.Text(), nullable=True),
        sa.Column('role', sa.Text(), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'idx_roast_conv_lookup',
        'roast_conversations',
        ['user_id', 'roast_id', 'created_at'],
    )


def downgrade() -> None:
    op.drop_index('idx_roast_conv_lookup', table_name='roast_conversations')
    op.drop_table('roast_conversations')
