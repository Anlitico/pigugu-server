"""RoastResult — persisted settlement data served to the App."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base


class RoastResult(Base):
    __tablename__ = "roast_results"

    roast_instance_id: Mapped[str] = mapped_column(
        String, primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    turn_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    best_take: Mapped[str | None] = mapped_column(Text)
    viewed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    settled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
