"""add roast_director_logs table

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-06-18 10:10:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text


# revision identifiers, used by Alembic.
revision: str = 'e5f6a7b8c9d0'
down_revision: Union[str, None] = 'd4e5f6a7b8c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    exists = conn.execute(
        text("SELECT EXISTS (SELECT FROM pg_tables WHERE tablename = 'roast_director_logs')")
    ).scalar()
    if not exists:
        op.create_table(
            'roast_director_logs',
            sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
            sa.Column('roast_instance_id', sa.Text(), nullable=False),
            sa.Column('turn_number', sa.Integer(), nullable=False),
            sa.Column('action', sa.Text(), nullable=True),
            sa.Column('best_take', sa.Text(), nullable=True),
            sa.Column('prompt', sa.Text(), nullable=True),
            sa.Column('close', sa.Boolean(), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index(
            'idx_director_logs_roast',
            'roast_director_logs',
            ['roast_instance_id', 'turn_number'],
        )


def downgrade() -> None:
    op.drop_index('idx_director_logs_roast', table_name='roast_director_logs')
    op.drop_table('roast_director_logs')
