"""add raw_articles table + expires_at index on roast_scenarios

Revision ID: j0k1l2m3n4o5
Revises: i9j0k1l2m3n4
Create Date: 2026-06-29 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision: str = "j0k1l2m3n4o5"
down_revision: Union[str, None] = "i9j0k1l2m3n4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "raw_articles",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.func.gen_random_uuid()),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("article_id", sa.String(256), nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("summary", sa.Text, nullable=False, server_default=sa.text("''")),
        sa.Column("url", sa.Text, nullable=False, server_default=sa.text("''")),
        sa.Column("category", sa.Text, nullable=False, server_default=sa.text("''")),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
        sa.UniqueConstraint("source", "article_id",
                            name="uq_raw_articles_source_article_id"),
    )
    op.create_index("idx_raw_articles_source", "raw_articles", ["source"])
    op.create_index("idx_raw_articles_published_at", "raw_articles", ["published_at"])

    # Add expires_at index for efficient scenario filtering
    op.create_index(
        "idx_roast_scenarios_active_expires",
        "roast_scenarios",
        ["status", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_roast_scenarios_active_expires", table_name="roast_scenarios")
    op.drop_index("idx_raw_articles_published_at", table_name="raw_articles")
    op.drop_index("idx_raw_articles_source", table_name="raw_articles")
    op.drop_table("raw_articles")
