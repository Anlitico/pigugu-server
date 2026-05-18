from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.roast_scenario import RoastScenario


async def list_active_scenarios(
    db: AsyncSession,
    mode: str | None = None,
) -> list[dict]:
    """Return active scenarios (14-day window, no prompt field)."""
    query = (
        select(
            RoastScenario.roast_id,
            RoastScenario.game_mode,
            RoastScenario.headline,
            RoastScenario.source,
            RoastScenario.source_url,
            RoastScenario.teaser,
            RoastScenario.tags,
            RoastScenario.is_urgent,
            RoastScenario.created_at,
            RoastScenario.expires_at,
        )
        .where(RoastScenario.status == "active")
        .where(RoastScenario.headline != "")
        .where(
            (RoastScenario.expires_at.is_(None))
            | (RoastScenario.expires_at > datetime.now(timezone.utc))
        )
        .where(
            RoastScenario.created_at
            > datetime.now(timezone.utc) - text("INTERVAL '14 days'")
        )
        .order_by(
            text(
                "CASE game_mode "
                "WHEN 'breaking_bomb' THEN 0 "
                "ELSE 1 END"
            ),
            RoastScenario.created_at.desc(),
        )
    )

    if mode:
        query = query.where(RoastScenario.game_mode == mode)

    result = await db.execute(query)
    rows = result.mappings().all()
    return [
        {
            "roast_id": r["roast_id"],
            "game_mode": r["game_mode"],
            "headline": r["headline"],
            "source": r["source"],
            "source_url": r["source_url"],
            "teaser": r["teaser"],
            "tags": r["tags"] if r["tags"] else [],
            "is_urgent": bool(r["is_urgent"]),
            "created_at": r["created_at"],
            "expires_at": r["expires_at"],
        }
        for r in rows
    ]


async def get_scenario_detail(
    db: AsyncSession,
    roast_id: str,
) -> dict | None:
    """Return a single scenario with full prompt."""
    result = await db.execute(
        select(RoastScenario).where(RoastScenario.roast_id == roast_id)
    )
    row = result.scalar_one_or_none()
    if row is None:
        return None
    return {
        "roast_id": row.roast_id,
        "game_mode": row.game_mode,
        "headline": row.headline,
        "source": row.source,
        "source_url": row.source_url,
        "teaser": row.teaser,
        "prompt": row.prompt,
        "tags": row.tags if row.tags else [],
        "is_urgent": bool(row.is_urgent),
        "created_at": row.created_at,
        "expires_at": row.expires_at,
    }
