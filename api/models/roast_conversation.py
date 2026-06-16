"""RoastConversation — persisted user/assistant messages for App display."""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base


class RoastConversation(Base):
    __tablename__ = "roast_conversations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    roast_id: Mapped[str] = mapped_column(Text, nullable=False)
    roast_instance_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index("idx_roast_conv_lookup", "user_id", "roast_id", "created_at"),
    )
