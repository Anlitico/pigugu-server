import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func, Index, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database import Base


class Device(Base):
    __tablename__ = "devices"
    __allow_unmapped__ = True
    __table_args__ = (
        Index("uq_devices_one_active_per_user", "user_id", unique=True, postgresql_where=(text("active_state = 'active'"))),
        Index("uq_devices_hardware_id_ci", text("lower(trim(hardware_id))"), unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    device_name: Mapped[str] = mapped_column(String(100), nullable=False)
    hardware_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    connectivity_status: Mapped[str] = mapped_column(String(16), default="unknown", nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_rtt_ms: Mapped[int | None] = mapped_column()
    active_state: Mapped[str] = mapped_column(String(16), default="standby", nullable=False)
    binding_status: Mapped[str] = mapped_column(String(16), default="bound", nullable=False)
    livekit_room_name: Mapped[str | None] = mapped_column(String(255))
    certificate_arn: Mapped[str | None] = mapped_column(String(512))
    thing_name: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Transient attribute (set by service layer, not persisted)
    is_online: bool

    user: Mapped["User"] = relationship("User", back_populates="devices")
    conversations: Mapped[list["Conversation"]] = relationship("Conversation", back_populates="device")
