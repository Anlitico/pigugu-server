"""create roast_history table if it doesn't exist

Revision ID: d4e5f6a7b8c9
Revises: b2c3d4e5f6a7
Create Date: 2026-06-18 07:30:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB, UUID


# revision identifiers, used by Alembic.
revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    exists = conn.execute(
        text("SELECT EXISTS (SELECT FROM pg_tables WHERE tablename = 'roast_history')")
    ).scalar()
    if not exists:
        op.create_table(
            'roast_history',
            sa.Column('roast_instance_id', sa.String(), primary_key=True),
            sa.Column('user_id', UUID(), nullable=False, index=True),
            sa.Column('roast_id', sa.String(), nullable=False),
            sa.Column('mode', sa.String(32), nullable=False, server_default='roast_together'),
            sa.Column('headline', sa.Text(), nullable=False, server_default=''),
            sa.Column('source', sa.Text(), nullable=False, server_default=''),
            sa.Column('turn_count', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('best_take', sa.Text(), nullable=True),
            sa.Column('interrupted', sa.Boolean(), nullable=False, server_default='f'),
            sa.Column('score_breakdown', JSONB(), nullable=True),
            sa.Column('viewed', sa.Boolean(), nullable=False, server_default='f'),
            sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('settled_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        )


def downgrade() -> None:
    op.drop_table('roast_history')
