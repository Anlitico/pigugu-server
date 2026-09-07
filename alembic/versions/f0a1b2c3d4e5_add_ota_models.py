"""add firmware_versions + device_ota_jobs + devices firmware columns

Revision ID: f0a1b2c3d4e5
Revises: k1l2m3n4o5p6
Create Date: 2026-09-07
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "f0a1b2c3d4e5"
down_revision = "k1l2m3n4o5p6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "firmware_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("board_target", sa.String(64), nullable=False, server_default="lichuang-dev"),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("git_sha", sa.String(64), nullable=True),
        sa.Column("file_key", sa.String(512), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("signature", sa.String(1024), nullable=False),
        sa.Column("release_notes", sa.Text(), nullable=True),
        sa.Column("visibility", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("released_by", sa.String(128), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("board_target", "version", name="uq_firmware_versions_board_version"),
    )

    op.create_table(
        "device_ota_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "device_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("devices.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "firmware_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("firmware_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("force", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("requested_by", sa.String(128), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="requested"),
        sa.Column("progress_pct", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status_detail", sa.String(512), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_device_ota_jobs_device_status", "device_ota_jobs", ["device_id", "status"]
    )
    op.create_index(
        "uq_device_ota_jobs_one_active",
        "device_ota_jobs",
        ["device_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('requested','notified','downloading','installing','rebooting')"
        ),
    )

    op.add_column("devices", sa.Column("current_firmware_version", sa.String(32), nullable=True))
    op.add_column("devices", sa.Column("current_firmware_sha", sa.String(64), nullable=True))
    op.add_column("devices", sa.Column("last_firmware_report_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("devices", "last_firmware_report_at")
    op.drop_column("devices", "current_firmware_sha")
    op.drop_column("devices", "current_firmware_version")
    op.drop_index("uq_device_ota_jobs_one_active", table_name="device_ota_jobs")
    op.drop_index("ix_device_ota_jobs_device_status", table_name="device_ota_jobs")
    op.drop_table("device_ota_jobs")
    op.drop_table("firmware_versions")
