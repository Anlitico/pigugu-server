"""add roast_scenarios table

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-17

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "roast_scenarios",
        sa.Column("roast_id", sa.Text, primary_key=True),
        sa.Column("game_mode", sa.String(20), nullable=False),
        sa.Column("prompt", sa.Text, nullable=False),
        sa.Column("news_id", sa.Text, nullable=True, server_default=sa.text("''")),
        sa.Column("tags", postgresql.JSONB, nullable=True, server_default=sa.text("'[]'::jsonb")),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'active'")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("idx_roast_scenarios_mode", "roast_scenarios", ["game_mode", "status"])


def downgrade() -> None:
    op.drop_index("idx_roast_scenarios_mode", table_name="roast_scenarios")
    op.drop_table("roast_scenarios")
