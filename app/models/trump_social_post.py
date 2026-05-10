import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class TrumpSocialPost(Base):
    __tablename__ = "trump_social_posts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    platform: Mapped[str] = mapped_column(String(20), nullable=False)
    post_id: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(String(2048))
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    crawled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    replies_count: Mapped[int | None] = mapped_column(Integer)
    reblogs_count: Mapped[int | None] = mapped_column(Integer)
    favourites_count: Mapped[int | None] = mapped_column(Integer)
    upvotes_count: Mapped[int | None] = mapped_column(Integer)
    media_attachments: Mapped[dict | None] = mapped_column(JSONB)
    tags: Mapped[list | None] = mapped_column(JSONB)
    mentions: Mapped[list | None] = mapped_column(JSONB)
    raw_payload: Mapped[dict | None] = mapped_column(JSONB)
    inserted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("platform", "post_id", name="uq_trump_social_posts_platform_post_id"),
    )
