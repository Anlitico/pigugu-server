"""add card-render columns to roast_scenarios

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-18

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "roast_scenarios",
        sa.Column("headline", sa.Text, nullable=False, server_default=sa.text("''")),
    )
    op.add_column(
        "roast_scenarios",
        sa.Column("source", sa.Text, nullable=False, server_default=sa.text("''")),
    )
    op.add_column(
        "roast_scenarios",
        sa.Column("source_url", sa.Text, nullable=False, server_default=sa.text("''")),
    )
    op.add_column(
        "roast_scenarios",
        sa.Column("teaser", sa.Text, nullable=False, server_default=sa.text("''")),
    )
    op.add_column(
        "roast_scenarios",
        sa.Column(
            "is_urgent", sa.Boolean, nullable=False, server_default=sa.text("FALSE")
        ),
    )


def downgrade() -> None:
    op.drop_column("roast_scenarios", "is_urgent")
    op.drop_column("roast_scenarios", "teaser")
    op.drop_column("roast_scenarios", "source_url")
    op.drop_column("roast_scenarios", "source")
    op.drop_column("roast_scenarios", "headline")
