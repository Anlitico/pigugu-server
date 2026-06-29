"""Activate a roast game — pure side effects, no PG query.

Entry point for both API-driven and function-call-driven roast starts.
Callers are responsible for loading the roast scenario from PG first,
then passing the data here.
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from prompts import PromptStore

from loguru import logger

from roast.constants import ROAST_BODY_PREFIX
from roast.state import RoastState
from roast.registry import GameModeRegistry


def _resolve_game_mode(db_game_mode: str):
    """Resolve a DB game_mode string to a GameMode instance."""
    return GameModeRegistry.get(db_game_mode)


async def activate_roast(
    *,
    user_id: str,
    persona_id: int,
    roast_id: str,
    game_mode: str,
    prompt: str,
    headline: str = "",
    source: str = "",
    redis,
    pg_pool=None,
    prompt_store: PromptStore | None = None,
) -> tuple[str, str]:
    """Start a roast game session and build the context body.

    Side effects:
    1. Creates RoastState in Redis (closes any previous active roast for this user).
    2. Returns the formatted roast body ready for ctx.add_turn().

    Args:
        user_id: The user starting the roast.
        persona_id: Persona ID for the game session.
        roast_id: DB roast_scenarios.roast_id.
        game_mode: DB roast_scenarios.game_mode (e.g. "debate", "roast_together").
        prompt: Full English game scenario prompt.
        redis: Redis client.
        pg_pool: Optional PG pool for RoastState history persistence.
        prompt_store: PromptStore for lazy prompt loading.

    Returns:
        (roast_instance_id, formatted_roast_body)
    """
    mode = _resolve_game_mode(game_mode)

    extra = mode.init_extra()
    extra["headline"] = headline
    extra["source"] = source

    state = await RoastState.start(
        user_id=user_id,
        persona_id=persona_id,
        roast_id=roast_id,
        mode=mode.mode,
        extra=extra,
        redis=redis,
        pg_pool=pg_pool,
    )

    body = f"{ROAST_BODY_PREFIX}\n{await _build_roast_body(game_mode_obj=mode, prompt=prompt, prompt_store=prompt_store)}"

    logger.info(
        f"[activate_roast] Started: {state.roast_instance_id} "
        f"roast_id={roast_id} mode={mode.mode} user={user_id}"
    )

    return state.roast_instance_id, body


async def _build_roast_body(*, game_mode_obj: Any, prompt: str = "", prompt_store: PromptStore | None = None) -> str:
    parts: list[str] = []
    if prompt.strip():
        parts.append(f"## News Context\n{prompt.strip()}")
    if prompt_store:
        ext = await game_mode_obj.get_system_prompt_extension(prompt_store)
    else:
        ext = ""
    if ext:
        parts.append(f"## Game Mode\n{ext}")
    return "\n\n".join(parts)
