"""add certificate_and_thing columns

Revision ID: 849d031a033c
Revises: 56cca7669edf
Create Date: 2026-05-07 16:52:25.733189

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '849d031a033c'
down_revision: Union[str, None] = '56cca7669edf'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("devices", sa.Column("certificate_arn", sa.String(512), nullable=True))
    op.add_column("devices", sa.Column("thing_name", sa.String(128), nullable=True))
    op.add_column("device_provisioning_sessions", sa.Column("certificate_arn", sa.String(512), nullable=True))


def downgrade() -> None:
    op.drop_column("device_provisioning_sessions", "certificate_arn")
    op.drop_column("devices", "thing_name")
    op.drop_column("devices", "certificate_arn")
