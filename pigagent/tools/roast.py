"""Roast Tools — list active roasts and start a roast game."""

from __future__ import annotations

import contextvars
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from typing import Any

from loguru import logger

from core.agent.tool import Tool
from roast.constants import TOOL_MARK_ROAST_COMPLETE

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
                    "enum": ["roast_together", "debate"],
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
    ctx,  # ContextManager — required, for persisting roast body
    redis=None,
    connect: ConnectFn | None = None,
) -> Tool:
    """Create a start_roast Tool that loads a roast scenario and starts the game.

    The handler:
    1. Queries PG for the roast scenario.
    2. Calls activate_roast() to create RoastState (Redis) and build the body.
    3. Persists the roast body to agent_conversations via ctx.add_turn() so
       subsequent turns inherit roast_instance_id.
    4. Returns _inject to insert the roast body into the LLM context,
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
                "SELECT roast_id, game_mode, prompt, headline, teaser, source "
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
                headline=row.get("headline", ""),
                teaser=row.get("teaser", ""),
                source=row.get("source", ""),
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

        # 3. Persist roast body to agent_conversations so subsequent turns
        #    inherit roast_instance_id via _assign_roast_instance_id().
        try:
            await ctx.add_turn(
                user_id=user_id,
                role="system",
                content=body,
                roast_instance_id=instance_id,
            )
        except Exception as e:
            logger.error(f"[start_roast] Failed to persist roast body: {e}")

        # 4. Return with _inject — runner injects roast body after tool_result
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
    """Create a mark_roast_complete Tool to end the current roast game.

    Called by the agent when: (1) the Director has signalled closing and
    the closing statement is complete (end_reason="completed"), or
    (2) the user wants to quit mid-game (end_reason="quit").

    best_take is read from state.extra (set by Director during the roast) —
    the agent does NOT pass it.

    Handler:
    1. Loads the active roast state from Redis.
    2. Transitions phase from ACTIVE/CLOSING to SETTLED.
    3. Writes roast_history to PG.
    4. Pushes settlement event to App via Redis (ws:user:{uid}).
    """

    async def _handler(args: dict) -> dict[str, Any]:
        from roast.state import RoastState
        from roast.types import Phase, Mode
        from roast.registry import GameModeRegistry

        user_id = _current_user_id.get()
        if not user_id:
            return {"settled": False, "reason": "no active user"}

        state = await RoastState._load_active(user_id, redis)
        if not state:
            return {"settled": False, "reason": "no active roast"}
        if state.phase not in (Phase.ACTIVE, Phase.CLOSING):
            return {"settled": False, "reason": f"roast already settled or closed: {state.phase}"}

        end_reason = args.get("end_reason", "completed")
        state.phase = Phase.SETTLED
        state.extra["settled"] = True

        # best_take is set by Director during the roast — agent does not pass it
        best_take = (state.extra.get("best_take") or "").strip()
        if best_take.lower() == "null":
            best_take = ""

        await state.save(redis, pg_pool)

        # Compute mode-specific summary for the App settlement push
        game_mode = GameModeRegistry.get(state.mode)
        score_data = game_mode.score(state)

        if state.mode == Mode.ROAST_TOGETHER:
            settlement_event = {
                "type": "roast_end",
                "roast_instance_id": state.roast_instance_id,
                "roast_id": state.roast_id,
                "mode_id": str(state.mode),
                "headline": state.extra.get("headline", ""),
                "source": state.extra.get("source", ""),
                "total_score": score_data.get("total_score", 0),
                "avg_score": score_data.get("avg_score", 0.0),
                "total_rounds": state.turn_count,
                "best_quote": score_data.get("best_quote") or None,
                "best_rating": score_data.get("best_rating", "meh"),
                "achievement_unlocked": None,
                "end_reason": end_reason,
                "started_at": state.started_at,
            }
        elif state.mode == Mode.DEBATE:
            settlement_event = {
                "type": "debate_end",
                "roast_instance_id": state.roast_instance_id,
                "roast_id": state.roast_id,
                "mode_id": str(state.mode),
                "headline": state.extra.get("headline", ""),
                "source": state.extra.get("source", ""),
                "final_user_support": score_data.get("final_user_support", 50.0),
                "result": score_data.get("debate_result", "draw"),
                "total_rounds": state.turn_count,
                "achievement_unlocked": None,
                "end_reason": end_reason,
                "started_at": state.started_at,
            }
        else:
            # Unknown/legacy mode — fall back to generic settlement event.
            # The mode-specific data (total_score, final_user_support, etc.)
            # won't be available, but the App still receives the settlement
            # notification with the basic fields it already handles.
            logger.warning(
                f"[mark_roast_complete] Unknown mode: {state.mode} — "
                f"falling back to generic roast_settled event"
            )
            settlement_event = {
                "type": "roast_settled",
                "roast_instance_id": state.roast_instance_id,
                "roast_id": state.roast_id,
                "mode": str(state.mode),
                "headline": state.extra.get("headline", ""),
                "source": state.extra.get("source", ""),
                "turn_count": state.turn_count,
                "best_take": best_take or None,
                "end_reason": end_reason,
                "started_at": state.started_at,
            }
        # Write roast_history (basic metadata only — per-round detail is in director_logs)
        try:
            if pg_pool is not None:
                from bootstrap.factory import get_pg_pool as _get_pool
                pool = pg_pool if hasattr(pg_pool, 'acquire') else await _get_pool()
                async with pool.acquire() as conn:
                    await conn.execute(
                        """INSERT INTO roast_history
                           (roast_instance_id, user_id, roast_id, mode, headline, source,
                            turn_count, best_take, interrupted, started_at, settled_at)
                           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9,
                                   to_timestamp($10), NOW())
                           ON CONFLICT (roast_instance_id) DO UPDATE SET
                            turn_count = EXCLUDED.turn_count,
                            best_take = EXCLUDED.best_take,
                            interrupted = EXCLUDED.interrupted,
                            settled_at = NOW()""",
                        state.roast_instance_id, user_id, state.roast_id,
                        str(state.mode), state.extra.get("headline", ""),
                        state.extra.get("source", ""), state.turn_count,
                        best_take or None, end_reason == "quit",
                        state.started_at,
                    )
        except Exception as e:
            logger.warning(f"[mark_roast_complete] roast_history write failed: {e}")

        # Push settlement summary to App via WS (Redis → ws_manager → all user devices).
        # Only for completed roasts — quit/interrupted does NOT send a card.
        if end_reason == "completed":
            import json as _json
            try:
                if redis is not None:
                    await redis.publish(
                        f"ws:user:{user_id}",
                        _json.dumps(settlement_event),
                    )
            except Exception as e:
                logger.warning(f"[mark_roast_complete] Redis publish failed: {e}")

        logger.info(
            f"[mark_roast_complete] Roast settled: {state.roast_instance_id} "
            f"type={settlement_event['type']} turns={state.turn_count} "
            f"end_reason={end_reason} user={user_id}"
        )

        return {"settled": True, "end_reason": end_reason}

    return Tool(
        name=TOOL_MARK_ROAST_COMPLETE,
        description=(
            "End the current roast game. Call this when: "
            "(1) the Director has signalled closing and you have finished your "
            "closing statement — use end_reason='completed', or "
            "(2) the user wants to quit mid-game — use end_reason='quit'. "
            "Do NOT call this during normal conversation when no roast is active."
        ),
        parameters={
            "type": "object",
            "properties": {
                "filler_text": {
                    "type": "string",
                    "description": "A brief spoken sentence to fill silence while the tool runs. Already spoken — do NOT repeat in your response.",
                },
                "end_reason": {
                    "type": "string",
                    "enum": ["completed", "quit"],
                    "description": "Why the roast is ending. 'completed' = natural close after Director signal + closing statement. 'quit' = user requested to stop early.",
                },
            },
            "required": ["filler_text", "end_reason"],
        },
        execute=_handler,
    )
