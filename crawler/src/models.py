"""ORM models shared with pigugu-server.

These mirror api/models/roast_scenario.py and define the new raw_articles table.
The models are intentionally thin — only the fields the crawler needs.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class RawArticle(Base):
    """New table for AP / Reuters headlines (PRD §3.3)."""

    __tablename__ = "raw_articles"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    source: Mapped[str] = mapped_column(String(16), nullable=False)  # ap | reuters
    article_id: Mapped[str] = mapped_column(String(256), nullable=False)  # GUID / URL hash
    title: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    url: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    category: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("source", "article_id", name="uq_raw_articles_source_article_id"),
        Index("idx_raw_articles_source", "source"),
        Index("idx_raw_articles_published_at", "published_at"),
    )


class RoastScenario(Base):
    """Mirror of api/models/roast_scenario.py — fields the crawler writes to."""

    __tablename__ = "roast_scenarios"

    roast_id: Mapped[str] = mapped_column(String, primary_key=True)
    game_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    headline: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    source_url: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    teaser: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    is_urgent: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("FALSE")
    )
    news_id: Mapped[str] = mapped_column(String, nullable=False, server_default=text("''"))
    tags: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
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
