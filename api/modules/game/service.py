import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.achievement import Achievement
from models.conversation import Conversation
from models.roast_result import RoastResult

logger = logging.getLogger(__name__)


def _format_result(row: RoastResult) -> dict:
    return {
        "roast_instance_id": row.roast_instance_id,
        "roast_id": row.roast_id,
        "mode": row.mode,
        "headline": row.headline,
        "source": row.source,
        "turn_count": row.turn_count,
        "best_take": row.best_take,
        "interrupted": row.interrupted,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "settled_at": row.settled_at.isoformat() if row.settled_at else None,
    }


async def get_roast_result(
    db: AsyncSession, user_id: uuid.UUID, roast_instance_id: str,
) -> dict | None:
    """Return settlement data for a completed roast."""
    result = await db.execute(
        select(RoastResult).where(
            RoastResult.roast_instance_id == roast_instance_id,
            RoastResult.user_id == user_id,
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        return None

    return _format_result(row)


async def get_pending_roasts(
    db: AsyncSession, user_id: uuid.UUID,
) -> list[dict]:
    """Return unviewed roast results for the user."""
    result = await db.execute(
        select(RoastResult)
        .where(RoastResult.user_id == user_id, RoastResult.viewed == False)
        .order_by(RoastResult.settled_at.desc())
        .limit(20)
    )
    rows = result.scalars().all()
    return [_format_result(row) for row in rows]


async def mark_roast_viewed(
    db: AsyncSession, user_id: uuid.UUID, roast_instance_id: str,
) -> None:
    """Mark a roast result as viewed by the user."""
    result = await db.execute(
        select(RoastResult).where(
            RoastResult.roast_instance_id == roast_instance_id,
            RoastResult.user_id == user_id,
        )
    )
    row = result.scalar_one_or_none()
    if row:
        row.viewed = True
        await db.commit()


async def get_game_state(db: AsyncSession, user_id: uuid.UUID) -> dict:
    ...


async def list_conversations(db: AsyncSession, user_id: uuid.UUID) -> list[Conversation]:
    ...


async def process_conversation_result(
    db: AsyncSession, conversation_id: uuid.UUID, outcome: str, score_delta: int
) -> None:
    ...


async def get_daily_leaderboard() -> list[dict]:
    ...


async def get_achievements(db: AsyncSession, user_id: uuid.UUID) -> list[Achievement]:
    ...


async def handle_roast_settled(
    db: AsyncSession,
    user_id: uuid.UUID,
    roast_instance_id: str,
    roast_id: str = "",
    mode: str = "",
    headline: str = "",
    source: str = "",
    turn_count: int = 0,
    best_take: str | None = None,
    interrupted: bool = False,
    started_at: float | None = None,
) -> None:
    """Called when a roast settles: persist result + WS broadcast + FCM push."""
    from datetime import datetime, timezone

    logger.info(
        "Roast settled: user=%s roast=%s mode=%s turns=%d has_best_take=%s interrupted=%s",
        user_id, roast_instance_id, mode, turn_count, bool(best_take), interrupted,
    )

    started_dt = (
        datetime.fromtimestamp(started_at, tz=timezone.utc)
        if started_at else None
    )

    # Write settlement result to DB
    db.add(RoastResult(
        roast_instance_id=roast_instance_id,
        user_id=user_id,
        roast_id=roast_id,
        mode=mode,
        headline=headline,
        source=source,
        turn_count=turn_count,
        best_take=best_take,
        interrupted=interrupted,
        started_at=started_dt,
    ))
    await db.commit()

    # WS broadcast — notify online App immediately
    try:
        from modules.ws.manager import ws_manager
        import json as _json5
        await ws_manager.broadcast_to_user(str(user_id), _json5.dumps({
            "type": "roast_settled",
            "roast_instance_id": roast_instance_id,
            "turn_count": turn_count,
            "best_take": best_take,
        }))
    except Exception as e:
        logger.warning("WS broadcast failed for user=%s: %s", user_id, e)

    # FCM push — fire-and-forget
    try:
        from modules.device.fcm import send_push

        if best_take:
            body = f"Your best take: \"{best_take[:80]}{'...' if len(best_take) > 80 else ''}\""
        else:
            body = f"Roast complete after {turn_count} rounds. Check your card!"

        await send_push(
            user_id,
            "Roast Complete 🎉",
            body,
            {
                "event": "roast_settled",
                "roast_instance_id": roast_instance_id,
                "turn_count": str(turn_count),
            },
        )
    except Exception as e:
        logger.warning("FCM push failed for user=%s: %s", user_id, e)
