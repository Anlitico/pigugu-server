"""add raw_result JSONB column to roast_director_logs

Revision ID: i9j0k1l2m3n4
Revises: g7h8i9j0k1l2
Create Date: 2026-06-29 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "i9j0k1l2m3n4"
down_revision: Union[str, None] = "g7h8i9j0k1l2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "roast_director_logs",
        sa.Column("raw_result", JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("roast_director_logs", "raw_result")
