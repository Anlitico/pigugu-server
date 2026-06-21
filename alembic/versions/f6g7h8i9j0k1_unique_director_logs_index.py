"""make roast_director_logs (roast_instance_id, turn_number) index unique

Revision ID: f6g7h8i9j0k1
Revises: e5f6a7b8c9d0
Create Date: 2026-06-21 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text


# revision identifiers, used by Alembic.
revision: str = 'f6g7h8i9j0k1'
down_revision: Union[str, None] = 'e5f6a7b8c9d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    # 1. Deduplicate existing rows before creating the unique index.
    #    Keep only the row with the highest id for each group.
    conn.execute(text("""
        DELETE FROM roast_director_logs
        WHERE id NOT IN (
            SELECT MAX(id)
            FROM roast_director_logs
            GROUP BY roast_instance_id, turn_number
        )
    """))

    # 2. Replace non-unique index with a unique one so that
    #    ON CONFLICT (roast_instance_id, turn_number) DO UPDATE works.
    op.drop_index('idx_director_logs_roast', table_name='roast_director_logs')
    op.create_index(
        'idx_director_logs_roast_unique',
        'roast_director_logs',
        ['roast_instance_id', 'turn_number'],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index('idx_director_logs_roast_unique', table_name='roast_director_logs')
    op.create_index(
        'idx_director_logs_roast',
        'roast_director_logs',
        ['roast_instance_id', 'turn_number'],
    )
