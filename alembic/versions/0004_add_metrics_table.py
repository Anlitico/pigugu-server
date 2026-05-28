"""add metrics and context_summaries tables

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-26

metrics: per-turn latency metrics for the voice pipeline (best-effort, fire-and-forget).
context_summaries: one row per compression run with l2_profile/l3_session/l4_roast layers.
  PK (user_id, end_turn) — latest query uses B-tree index, O(log n).
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0004"
down_revision: Union[str, None] = "0004_context"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    existing = insp.get_table_names()

    # ── metrics ──
    if "metrics" not in existing:
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

    # ── context_summaries (redesigned: one row per compression run) ──
    if "context_summaries" not in existing:
        op.create_table(
            "context_summaries",
            sa.Column("user_id", sa.Text, primary_key=True),
            sa.Column("end_turn", sa.Integer, primary_key=True),
            sa.Column("l2_profile", sa.Text, nullable=False, server_default=""),
            sa.Column("l3_session", sa.Text, nullable=False, server_default=""),
            sa.Column("l4_roast", sa.Text, nullable=False, server_default=""),
            sa.Column("roast_id", sa.Text, nullable=True),
            sa.Column("roast_prompt", sa.Text, nullable=False, server_default=""),
            sa.Column("roast_prompt_turn", sa.Integer, nullable=False, server_default="0"),
            sa.Column("model_used", sa.Text, nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
    else:
        # Table exists from earlier migration — add missing columns
        cols = {c["name"] for c in insp.get_columns("context_summaries")}
        if "roast_prompt" not in cols:
            op.add_column("context_summaries", sa.Column("roast_prompt", sa.Text, nullable=False, server_default=""))
        if "roast_prompt_turn" not in cols:
            op.add_column("context_summaries", sa.Column("roast_prompt_turn", sa.Integer, nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_table("context_summaries")
    op.drop_table("metrics")
