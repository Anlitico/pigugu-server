import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func, Index, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base

# Lifecycle: requested → notified → downloading → installing → rebooting → succeeded
# Branching terminal / cancelled paths:
#   downloading/installing/rebooting → failed (device reported, or server OTA_JOB_TIMEOUT_SECS elapsed)
#   any active state + device self-rollback       → rolled_back
#   superseded by newer target / unbind / on-target shortcut → superseded (historical, not a user-facing failure)
REQUESTED = "requested"
NOTIFIED = "notified"
DOWNLOADING = "downloading"
INSTALLING = "installing"
REBOOTING = "rebooting"
SUCCEEDED = "succeeded"
FAILED = "failed"
ROLLED_BACK = "rolled_back"
SUPERSEDED = "superseded"

ACTIVE_STATUSES = (REQUESTED, NOTIFIED, DOWNLOADING, INSTALLING, REBOOTING)
TERMINAL_STATUSES = (SUCCEEDED, FAILED, ROLLED_BACK, SUPERSEDED)


class DeviceOtaJob(Base):
    __tablename__ = "device_ota_jobs"
    __allow_unmapped__ = True
    __table_args__ = (
        Index("ix_device_ota_jobs_device_status", "device_id", "status"),
        # One device upgrades one target at a time — enforced at the DB, not
        # just the app layer (concurrent upgrade POSTs can otherwise orphan a
        # second "active" requested row that nothing ever cleans up).
        Index(
            "uq_device_ota_jobs_one_active",
            "device_id",
            unique=True,
            postgresql_where=text(
                "status IN ('requested','notified','downloading','installing','rebooting')"
            ),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    firmware_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("firmware_versions.id", ondelete="RESTRICT"), nullable=False
    )
    force: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    requested_by: Mapped[str | None] = mapped_column(String(128))  # user id or "script:<who>"
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=REQUESTED)
    progress_pct: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status_detail: Mapped[str | None] = mapped_column(String(512))
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
