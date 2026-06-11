"""Roast Tools — list active roasts and start a roast game."""

from __future__ import annotations

import contextvars
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from typing import Any

from loguru import logger

from core.agent.tool import Tool

ConnectFn = Callable[[str], Awaitable[Any]]


def _parse_date(val: str) -> date:
    return datetime.strptime(val, "%Y-%m-%d").date()

_current_user_id = contextvars.ContextVar("current_user_id", default="")
_current_persona_id = contextvars.ContextVar("current_persona_id", default=1)


def create_list_roasts_tool(pg_pool: str, *, connect: ConnectFn | None = None) -> Tool:
    """Create a list_active_roasts Tool that queries the PG roast_scenarios table."""

    async def _acquire():
        if connect is not None:
            return await connect(pg_pool)
        from context.storage.pg import _ensure_pg_pool
        pool = await _ensure_pg_pool()
        return await pool.acquire()

    async def _handler(args: dict) -> dict[str, Any]:
        game_mode = args.get("game_mode")
        start_date = args.get("start_date")
        end_date = args.get("end_date")

        query = """
            SELECT roast_id, game_mode, headline, teaser, created_at
            FROM roast_scenarios
            WHERE status = 'active'
        """
        params: list[Any] = []

        if game_mode:
            idx = len(params) + 1
            query += f" AND game_mode = ${idx}"
            params.append(game_mode)
        if start_date:
            idx = len(params) + 1
            query += f" AND created_at >= ${idx}"
            params.append(_parse_date(start_date))
        if end_date:
            idx = len(params) + 1
            query += f" AND created_at <= ${idx}"
            params.append(_parse_date(end_date))

        query += " ORDER BY created_at DESC LIMIT 50"

        conn = await _acquire()
        try:
            rows = await conn.fetch(query, *params)
            roasts = [
                {
                    "roast_id": row["roast_id"],
                    "game_mode": row["game_mode"],
                    "headline": row["headline"],
                    "teaser": row["teaser"],
                    "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                }
                for row in rows
            ]
            return {"total": len(roasts), "roasts": roasts}
        finally:
            await conn.close()

    return Tool(
        name="list_active_roasts",
        description=(
            "List currently active roast game scenarios. "
            "Returns roast_id, game_mode, headline, teaser, and created_at for each."
        ),
        parameters={
            "type": "object",
            "properties": {
                "filler_text": {
                    "type": "string",
                    "description": "A brief spoken sentence to fill silence while the tool runs. Already spoken — do NOT repeat in your response.",
                },
                "game_mode": {
                    "type": "string",
                    "enum": ["poison_opinion", "debate", "prediction", "breaking_bomb"],
                    "description": "Filter by game mode. Omit to return all modes.",
                },
                "start_date": {
                    "type": "string",
                    "description": "Filter roasts created on or after this date (YYYY-MM-DD format).",
                },
                "end_date": {
                    "type": "string",
                    "description": "Filter roasts created on or before this date (YYYY-MM-DD format).",
                },
            },
            "required": ["filler_text"],
        },
        execute=_handler,
    )


def create_start_roast_tool(
    pg_pool: str,
    *,
    redis=None,
    connect: ConnectFn | None = None,
) -> Tool:
    """Create a start_roast Tool that loads a roast scenario and starts the game.

    The handler:
    1. Queries PG for the roast scenario.
    2. Calls activate_roast() to create RoastState (Redis) and build the body.
    3. Returns _inject to insert the roast body after the tool_result,
       so the context order is: tool_call → tool_result → user(roast body) → assistant(opening).
    """

    async def _acquire():
        if connect is not None:
            return await connect(pg_pool)
        from context.storage.pg import _ensure_pg_pool
        pool = await _ensure_pg_pool()
        return await pool.acquire()

    async def _handler(args: dict) -> dict[str, Any]:
        from roast.activate import activate_roast

        roast_id = args["roast_id"]
        user_id = _current_user_id.get()
        if not user_id:
            return {"message": "Cannot start roast: no active user session."}
        persona_id = _current_persona_id.get()

        # 1. Load roast from PG
        conn = await _acquire()
        try:
            row = await conn.fetchrow(
                "SELECT roast_id, game_mode, prompt "
                "FROM roast_scenarios "
                "WHERE roast_id = $1 AND status = 'active'",
                roast_id,
            )
        finally:
            await conn.close()

        if row is None:
            return {"message": f"Roast not found or not active: {roast_id}"}

        # 2. Activate roast — creates RoastState in Redis, builds body
        try:
            instance_id, body = await activate_roast(
                user_id=user_id,
                persona_id=persona_id,
                roast_id=row["roast_id"],
                game_mode=row["game_mode"],
                prompt=row["prompt"],
                redis=redis,
                pg_pool=pg_pool,
            )
        except Exception as e:
            logger.error(f"[start_roast] activate_roast failed: {e}")
            return {"message": "Failed to start roast session."}

        logger.info(
            f"[start_roast] Roast activated: {instance_id} "
            f"roast_id={roast_id} user={user_id}"
        )

        # 3. Return with _inject — runner injects roast body after tool_result
        return {
            "message": (
                f"Roast loaded. The game scenario will be added to the context. "
                f"roast_instance_id: {instance_id}"
            ),
            "_inject": [
                {"role": "system", "content": body},
            ],
        }

    return Tool(
        name="start_roast",
        description=(
            "Start a roast game by roast_id. "
            "Loads the full scenario and the game begins immediately."
        ),
        parameters={
            "type": "object",
            "properties": {
                "filler_text": {
                    "type": "string",
                    "description": "A brief spoken sentence to fill silence while the game loads. Already spoken — do NOT repeat in your response.",
                },
                "roast_id": {
                    "type": "string",
                    "description": "The roast_id to start, e.g. debate_2026-05-19_001",
                },
            },
            "required": ["filler_text", "roast_id"],
        },
        execute=_handler,
    )


def create_roast_complete_tool(
    *,
    redis=None,
    pg_pool=None,
) -> Tool:
    """Create a mark_roast_complete Tool that the agent calls after its closing statement.

    The handler:
    1. Loads the active roast state from Redis.
    2. Transitions phase from CLOSING to SETTLED.
    3. Computes has_best_take from best_take_energy.
    4. Publishes a roast_settled event for App consumption.
    """

    async def _handler(args: dict) -> dict[str, Any]:
        from roast.state import RoastState
        from roast.types import Phase
        from roast.event_bus import event_bus

        user_id = _current_user_id.get()
        if not user_id:
            return {"settled": False, "reason": "no active user"}

        state = await RoastState._load_active(user_id, redis)
        if not state:
            return {"settled": False, "reason": "no active roast"}
        if state.phase not in (Phase.ACTIVE, Phase.CLOSING):
            return {"settled": False, "reason": f"roast already settled or closed: {state.phase}"}

        state.phase = Phase.SETTLED
        state.extra["settled"] = True
        await state.save(redis, pg_pool)

        # Notify App asynchronously — settlement card trigger
        await event_bus.publish(user_id, {
            "type": "roast_settled",
            "roast_instance_id": state.roast_instance_id,
            "turn_count": state.turn_count,
        })

        logger.info(
            f"[mark_roast_complete] Roast settled: {state.roast_instance_id} "
            f"turns={state.turn_count} user={user_id}"
        )

        return {"settled": True}

    return Tool(
        name="mark_roast_complete",
        description=(
            "Call this tool after you finish your closing statement in a roast game. "
            "Marks the roast as settled so the user can continue free chat."
        ),
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
        },
        execute=_handler,
    )
