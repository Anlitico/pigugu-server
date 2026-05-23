"""add context module tables

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-17

Five tables for the 4-layer agent context architecture:
  - agent_conversations: raw turns with full Message support
  - context_summaries: compressed summaries with turn-range anchors
  - user_facts: extracted discrete facts with categories
  - user_memory: user profile summary
  - roast_scenarios: game scenarios from crawler pipeline
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
    # ── agent_conversations ──
    op.create_table(
        "agent_conversations",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Text, nullable=False),
        sa.Column("turn_number", sa.Integer, nullable=False),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("content", sa.Text, nullable=False, server_default=""),
        sa.Column("tool_calls", postgresql.JSONB, nullable=True),
        sa.Column("tool_call_id", sa.Text, nullable=True),
        sa.Column("name", sa.Text, nullable=True),
        sa.Column("partial", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("roast_id", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "turn_number"),
    )
    op.create_index("idx_ac_user", "agent_conversations", ["user_id", "turn_number"])
    op.create_index(
        "idx_ac_roast", "agent_conversations", ["user_id", "roast_id"],
        postgresql_where=sa.text("roast_id IS NOT NULL"),
    )
    op.create_index(
        "idx_ac_tool_call", "agent_conversations", ["user_id", "tool_call_id"],
        postgresql_where=sa.text("tool_call_id IS NOT NULL"),
    )

    # ── context_summaries ──
    op.create_table(
        "context_summaries",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Text, nullable=False),
        sa.Column("summary_type", sa.Text, nullable=False),
        sa.Column("roast_id", sa.Text, nullable=False, server_default=""),
        sa.Column("tier", sa.Integer, nullable=False, server_default="1"),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("start_turn", sa.Integer, nullable=False, server_default="0"),
        sa.Column("end_turn", sa.Integer, nullable=False, server_default="0"),
        sa.Column("model_used", sa.Text, nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "idx_summaries_user", "context_summaries",
        ["user_id", "summary_type", "tier", sa.text("end_turn DESC")],
    )

    # ── user_facts ──
    op.create_table(
        "user_facts",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Text, nullable=False),
        sa.Column("fact", sa.Text, nullable=False),
        sa.Column("category", sa.Text, nullable=False, server_default="personal"),
        sa.Column("confidence", sa.Float, nullable=False, server_default="1.0"),
        sa.Column("source_turn", sa.Integer, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "fact"),
    )
    op.create_index("idx_facts_user", "user_facts", ["user_id", "category"])

    # ── user_memory ──
    op.create_table(
        "user_memory",
        sa.Column("user_id", sa.Text, primary_key=True),
        sa.Column("profile_summary", sa.Text, nullable=False, server_default=""),
        sa.Column("stats", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # ── roast_scenarios ──
    op.create_table(
        "roast_scenarios",
        sa.Column("roast_id", sa.Text, primary_key=True),
        sa.Column("game_mode", sa.Text, nullable=False),
        sa.Column("prompt", sa.Text, nullable=False),
        sa.Column("news_id", sa.Text, nullable=False, server_default=""),
        sa.Column("tags", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("status", sa.Text, nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "idx_roast_scenarios_mode", "roast_scenarios", ["game_mode", "status"],
    )


def downgrade() -> None:
    op.drop_table("roast_scenarios")
    op.drop_table("user_memory")
    op.drop_table("user_facts")
    op.drop_table("context_summaries")
    op.drop_table("agent_conversations")
