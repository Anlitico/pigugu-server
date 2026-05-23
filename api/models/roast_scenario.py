from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base


class RoastScenario(Base):
    __tablename__ = "roast_scenarios"

    roast_id: Mapped[str] = mapped_column(String, primary_key=True)
    game_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    headline: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("''")
    )
    source: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("''")
    )
    source_url: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("''")
    )
    teaser: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("''")
    )
    is_urgent: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("FALSE")
    )
    news_id: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("''")
    )
    tags: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'active'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("idx_roast_scenarios_mode", "game_mode", "status"),
    )
