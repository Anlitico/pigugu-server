"""Activate a roast game — pure side effects, no PG query.

Entry point for both API-driven and function-call-driven roast starts.
Callers are responsible for loading the roast scenario from PG first,
then passing the data here.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from roast.state import RoastState
from roast.registry import GameModeRegistry

# Map DB game_mode values → Mode enum strings.
# DB values come from the classifier (poison_opinion, debate, etc.)
# and don't match the Mode enum (roast_together, debate_bicker, etc.).
# This compatibility layer will be removed when DB and code are aligned.
_DB_MODE_MAP: dict[str, str] = {
    "poison_opinion": "roast_together",
    "debate": "debate_bicker",
    "breaking_bomb": "breaking_bomb",
}


def _resolve_game_mode(db_game_mode: str):
    """Resolve a DB game_mode string to a GameMode instance.

    First checks the compatibility map, then tries the string directly,
    and finally falls back to GameModeRegistry.get() (which falls back to
    roast_together for unknown values).
    """
    mapped = _DB_MODE_MAP.get(db_game_mode, db_game_mode)
    return GameModeRegistry.get(mapped)


async def activate_roast(
    *,
    user_id: str,
    persona_id: int,
    roast_id: str,
    game_mode: str,
    prompt: str,
    redis,
    pg_pool=None,
) -> tuple[str, str]:
    """Start a roast game session and build the context body.

    Side effects:
    1. Creates RoastState in Redis (closes any previous active roast for this user).
    2. Returns the formatted roast body ready for ctx.add_turn().

    Args:
        user_id: The user starting the roast.
        persona_id: Persona ID for the game session.
        roast_id: DB roast_scenarios.roast_id.
        game_mode: DB roast_scenarios.game_mode (e.g. "debate", "poison_opinion").
        prompt: Full English game scenario prompt.
        redis: Redis client.
        pg_pool: Optional PG pool for RoastState history persistence.

    Returns:
        (roast_instance_id, formatted_roast_body)
    """
    mode = _resolve_game_mode(game_mode)

    state = await RoastState.start(
        user_id=user_id,
        persona_id=persona_id,
        roast_id=roast_id,
        mode=mode.mode,
        extra=mode.init_extra(),
        redis=redis,
        pg_pool=pg_pool,
    )

    body = f"[Game Background]\n{_build_roast_body(game_mode_obj=mode, prompt=prompt)}"

    logger.info(
        f"[activate_roast] Started: {state.roast_instance_id} "
        f"roast_id={roast_id} mode={mode.mode} user={user_id}"
    )

    return state.roast_instance_id, body


def _build_roast_body(*, game_mode_obj: Any, prompt: str = "") -> str:
    parts: list[str] = []
    if prompt.strip():
        parts.append(f"## News Context\n{prompt.strip()}")
    ext = getattr(game_mode_obj, "system_prompt_extension", "")
    if ext:
        parts.append(f"## Game Mode\n{ext}")
    return "\n\n".join(parts)
