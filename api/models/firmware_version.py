import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base

# visibility lifecycle: draft → released → retired
DRAFT = "draft"
RELEASED = "released"
RETIRED = "retired"
VISIBILITY_STATES = (DRAFT, RELEASED, RETIRED)


class FirmwareVersion(Base):
    __tablename__ = "firmware_versions"
    __allow_unmapped__ = True
    __table_args__ = (
        UniqueConstraint("board_target", "version", name="uq_firmware_versions_board_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    board_target: Mapped[str] = mapped_column(String(64), nullable=False, default="lichuang-dev")
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    git_sha: Mapped[str | None] = mapped_column(String(64))
    file_key: Mapped[str] = mapped_column(String(512), nullable=False)  # S3 object key
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    signature: Mapped[str] = mapped_column(String(1024), nullable=False)  # BASE64 DER ECDSA-P256
    release_notes: Mapped[str | None] = mapped_column(Text)
    visibility: Mapped[str] = mapped_column(String(16), nullable=False, default=DRAFT)
    released_by: Mapped[str | None] = mapped_column(String(128))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
