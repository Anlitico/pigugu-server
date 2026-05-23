"""SQLAlchemy models for agent context module — 4-layer architecture."""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Float, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base


class AgentConversation(Base):
    __tablename__ = "agent_conversations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    turn_number: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    tool_calls: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    partial: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    roast_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("user_id", "turn_number", name="uq_agent_conversations_user_turn"),
    )


class ContextSummary(Base):
    __tablename__ = "context_summaries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    summary_type: Mapped[str] = mapped_column(Text, nullable=False)      # 'session' | 'roast'
    roast_id: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    tier: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")  # 1=recent, 2=global
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    start_turn: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    end_turn: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    model_used: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UserFact(Base):
    __tablename__ = "user_facts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    fact: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False, server_default="personal")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, server_default="1.0")
    source_turn: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("user_id", "fact", name="uq_user_facts_user_fact"),
    )


class UserMemory(Base):
    __tablename__ = "user_memory"

    user_id: Mapped[str] = mapped_column(Text, primary_key=True)
    profile_summary: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    stats: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RoastScenario(Base):
    __tablename__ = "roast_scenarios"

    roast_id: Mapped[str] = mapped_column(Text, primary_key=True)
    game_mode: Mapped[str] = mapped_column(Text, nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    news_id: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    tags: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
