"""add compression_metrics table

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-28

compression_metrics: per-compression-run timing and stats.
  - turn_id=0, kind="compression" to avoid conflicting with conversation metrics
  - segments store phase durations (check, llm, profile, total)
  - meta stores metadata (turns_in, turns_out, facts, model, etc.)
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    existing = insp.get_table_names()

    if "compression_metrics" not in existing:
        op.create_table(
            "compression_metrics",
            sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
            sa.Column("user_id", sa.Text, nullable=False),
            sa.Column("scenario", sa.Text, nullable=False, server_default="free_chat"),
            sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("segments", postgresql.JSONB, nullable=False, server_default="{}"),
            sa.Column("meta", postgresql.JSONB, nullable=False, server_default="{}"),
        )
        op.create_index("idx_comp_metrics_user_ts", "compression_metrics", ["user_id", sa.text("ts DESC")])


def downgrade() -> None:
    op.drop_table("compression_metrics")
