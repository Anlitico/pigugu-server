import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.achievement import Achievement
from app.models.conversation import Conversation


async def get_game_state(db: AsyncSession, user_id: uuid.UUID) -> dict:
    ...


async def list_conversations(db: AsyncSession, user_id: uuid.UUID) -> list[Conversation]:
    ...


async def process_conversation_result(
    db: AsyncSession, conversation_id: uuid.UUID, outcome: str, score_delta: int
) -> None:
    """Update credibility score, check achievement conditions, publish score_update event."""
    ...


async def get_daily_leaderboard() -> list[dict]:
    """Read from Redis Sorted Set `leaderboard:daily`."""
    ...


async def get_achievements(db: AsyncSession, user_id: uuid.UUID) -> list[Achievement]:
    ...
