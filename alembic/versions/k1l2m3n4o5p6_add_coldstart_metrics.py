"""add coldstart_metrics table

Revision ID: k1l2m3n4o5p6
Revises: j0k1l2m3n4o5
Create Date: 2026-07-18

coldstart_metrics: per-session cold-start latency for the LiveKit agent
  session bootstrap pipeline (entry → Agent ready).
  Best-effort, fire-and-forget — same pattern as turn metrics.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "k1l2m3n4o5p6"
down_revision: Union[str, None] = "j0k1l2m3n4o5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    existing = insp.get_table_names()

    if "coldstart_metrics" not in existing:
        op.create_table(
            "coldstart_metrics",
            sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
            sa.Column("session_id", sa.Text, nullable=False),
            sa.Column("room_name", sa.Text, nullable=False, server_default=""),
            sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("marks", postgresql.JSONB, nullable=False, server_default="{}"),
            sa.Column("segments", postgresql.JSONB, nullable=False, server_default="{}"),
            sa.Column("meta", postgresql.JSONB, nullable=False, server_default="{}"),
        )
        op.create_index("idx_coldstart_ts", "coldstart_metrics", [sa.text("ts DESC")])
        op.create_index("idx_coldstart_session", "coldstart_metrics", ["session_id"])


def downgrade() -> None:
    op.drop_table("coldstart_metrics")
