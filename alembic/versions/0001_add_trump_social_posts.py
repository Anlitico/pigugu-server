"""add trump_social_posts table

Revision ID: 0001
Revises:
Create Date: 2026-05-10

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "trump_social_posts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("platform", sa.String(20), nullable=False),
        sa.Column("post_id", sa.String(255), nullable=False),
        sa.Column("content", sa.Text, nullable=True),
        sa.Column("url", sa.String(2048), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("crawled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("replies_count", sa.Integer, nullable=True),
        sa.Column("reblogs_count", sa.Integer, nullable=True),
        sa.Column("favourites_count", sa.Integer, nullable=True),
        sa.Column("upvotes_count", sa.Integer, nullable=True),
        sa.Column("media_attachments", postgresql.JSONB, nullable=True),
        sa.Column("tags", postgresql.JSONB, nullable=True),
        sa.Column("mentions", postgresql.JSONB, nullable=True),
        sa.Column("raw_payload", postgresql.JSONB, nullable=True),
        sa.Column(
            "inserted_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "platform", "post_id", name="uq_trump_social_posts_platform_post_id"
        ),
    )
    op.create_index("ix_trump_social_posts_platform_created_at", "trump_social_posts", ["platform", "created_at"])
    op.create_index("ix_trump_social_posts_created_at", "trump_social_posts", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_trump_social_posts_created_at", table_name="trump_social_posts")
    op.drop_index("ix_trump_social_posts_platform_created_at", table_name="trump_social_posts")
    op.drop_table("trump_social_posts")
