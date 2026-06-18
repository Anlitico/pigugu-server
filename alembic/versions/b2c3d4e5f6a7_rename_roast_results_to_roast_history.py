"""rename roast_results → roast_history

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-06-16 14:05:00.000000

"""

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text


# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    row = conn.execute(
        text("SELECT EXISTS (SELECT FROM pg_tables WHERE tablename = 'roast_results')")
    ).scalar()
    if row:
        op.rename_table('roast_results', 'roast_history')


def downgrade() -> None:
    conn = op.get_bind()
    row = conn.execute(
        text("SELECT EXISTS (SELECT FROM pg_tables WHERE tablename = 'roast_history')")
    ).scalar()
    if row:
        op.rename_table('roast_history', 'roast_results')
