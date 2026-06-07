"""add fcm_tokens table

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-07

Store Firebase Cloud Messaging tokens for push notification delivery.
Each user can have multiple tokens (one per device). Token is unique
across all users to allow dedup on upsert.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    existing = insp.get_table_names()

    if "fcm_tokens" not in existing:
        op.create_table(
            "fcm_tokens",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "user_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("token", sa.String(512), nullable=False, unique=True),
            sa.Column("platform", sa.String(16), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
            ),
        )
        op.create_index("idx_fcm_tokens_user_id", "fcm_tokens", ["user_id"])


def downgrade() -> None:
    op.drop_index("idx_fcm_tokens_user_id", table_name="fcm_tokens")
    op.drop_table("fcm_tokens")
