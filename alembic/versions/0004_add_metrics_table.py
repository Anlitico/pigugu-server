"""add metrics table

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-26

Per-turn latency metrics for the voice pipeline.
Written asynchronously — best-effort, no transactional coupling.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "metrics",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Text, nullable=False),
        sa.Column("turn_id", sa.Integer, nullable=False),
        sa.Column("persona_id", sa.Integer, nullable=False, server_default="0"),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("marks", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("segments", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("meta", postgresql.JSONB, nullable=False, server_default="{}"),
    )
    op.create_index("idx_metrics_user_ts", "metrics", ["user_id", sa.text("ts DESC")])
    op.create_index("idx_metrics_ts", "metrics", [sa.text("ts DESC")])
    op.create_unique_constraint("uq_metrics_user_turn", "metrics", ["user_id", "turn_id"])


def downgrade() -> None:
    op.drop_table("metrics")
