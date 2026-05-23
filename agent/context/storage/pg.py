# agent/context/storage/pg.py
"""PostgreSQL I/O for context module — turns, facts, profile, recovery."""

from __future__ import annotations

import json
from datetime import datetime

from loguru import logger

from core.llm.types import Message
from context.schema import UserMemory


def _serialize_tool_calls(tool_calls: list | None) -> str | None:
    """Serialize ToolCall list to JSONB string for PG insert. Returns None if empty."""
    if not tool_calls:
        return None
    return json.dumps([
        {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
        for tc in tool_calls
    ])


class PgStorage:
    """PostgreSQL read/write helpers. Async with connection pool."""

    def __init__(self, user_id: str, pg_pool=None):
        self._user_id = user_id
        self._pg = pg_pool

    # ── Turns ──────────────────────────────────────────────────────

    async def flush_one(self, turn_number: int, turn: Message, roast_id: str | None) -> None:
        if not self._pg:
            return
        try:
            async with self._pg.acquire() as conn:
                await conn.execute(
                    """INSERT INTO agent_conversations
                       (user_id, turn_number, role, content,
                        tool_calls, tool_call_id, name, partial, roast_id)
                       VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9)
                       ON CONFLICT (user_id, turn_number) DO NOTHING""",
                    self._user_id, turn_number,
                    turn.role, turn.content,
                    _serialize_tool_calls(turn.tool_calls),
                    turn.tool_call_id, turn.name, turn.partial,
                    roast_id,
                )
        except Exception as e:
            logger.warning(f"PG flush_one failed: {e}")

    async def flush_buffer(self, batch: list[tuple[int, Message, str | None]]) -> None:
        if not self._pg:
            return
        try:
            async with self._pg.acquire() as conn:
                async with conn.transaction():
                    for turn_number, turn, roast_id in batch:
                        await conn.execute(
                            """INSERT INTO agent_conversations
                               (user_id, turn_number, role, content,
                                tool_calls, tool_call_id, name, partial, roast_id)
                               VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9)
                               ON CONFLICT (user_id, turn_number) DO NOTHING""",
                            self._user_id, turn_number,
                            turn.role, turn.content,
                            _serialize_tool_calls(turn.tool_calls),
                            turn.tool_call_id, turn.name, turn.partial,
                            roast_id,
                        )
            logger.debug(f"Flushed {len(batch)} turns to PG")
        except Exception as e:
            logger.warning(f"PG flush failed: {e}")

    async def persist_turns(self, turns: list[tuple[int, Message, str | None]]) -> None:
        if not self._pg:
            return
        try:
            async with self._pg.acquire() as conn:
                async with conn.transaction():
                    for turn_number, turn, roast_id in turns:
                        await conn.execute(
                            """INSERT INTO agent_conversations
                               (user_id, turn_number, role, content,
                                tool_calls, tool_call_id, name, partial, roast_id)
                               VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9)
                               ON CONFLICT (user_id, turn_number) DO NOTHING""",
                            self._user_id, turn_number,
                            turn.role, turn.content,
                            _serialize_tool_calls(turn.tool_calls),
                            turn.tool_call_id, turn.name, turn.partial,
                            roast_id,
                        )
            logger.info(f"Persisted {len(turns)} turns to PG")
        except Exception as e:
            logger.error(f"PG persist failed: {e}")

    async def recover_turn_counter(self) -> int:
        if not self._pg:
            return 0
        try:
            async with self._pg.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT COALESCE(MAX(turn_number), 0) FROM agent_conversations "
                    "WHERE user_id = $1",
                    self._user_id,
                )
                if row:
                    return row[0]
        except Exception as e:
            logger.warning(f"Failed to recover turn_counter from PG: {e}")
        return 0

    # ── Facts ──────────────────────────────────────────────────────

    async def persist_facts(self, fact_dicts: list[dict]) -> None:
        if not self._pg:
            return
        try:
            async with self._pg.acquire() as conn:
                async with conn.transaction():
                    for fd in fact_dicts:
                        await conn.execute(
                            """INSERT INTO user_facts (user_id, fact, category)
                               VALUES ($1, $2, $3)
                               ON CONFLICT (user_id, fact) DO NOTHING""",
                            self._user_id,
                            fd.get("fact", ""),
                            fd.get("category", "personal"),
                        )
        except Exception as e:
            logger.warning(f"Failed to persist facts: {e}")

    async def read_new_facts(self, since: datetime | None = None) -> list[str]:
        if not self._pg:
            return []
        try:
            async with self._pg.acquire() as conn:
                if since:
                    rows = await conn.fetch(
                        "SELECT fact, category FROM user_facts WHERE user_id=$1 "
                        "AND created_at > $2 ORDER BY created_at DESC",
                        self._user_id, since,
                    )
                else:
                    rows = await conn.fetch(
                        "SELECT fact, category FROM user_facts WHERE user_id=$1 "
                        "ORDER BY created_at DESC",
                        self._user_id,
                    )
                return [f"{row['fact']} ({row['category']})" for row in rows]
        except Exception:
            return []

    # ── Profile ────────────────────────────────────────────────────

    async def read_profile(self) -> tuple[str, datetime | None]:
        if not self._pg:
            return "", None
        try:
            async with self._pg.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT profile_summary, updated_at FROM user_memory WHERE user_id=$1",
                    self._user_id,
                )
                if row:
                    return row["profile_summary"] or "", row["updated_at"]
        except Exception:
            pass
        return "", None

    async def upsert_profile(self, profile: str) -> None:
        if not self._pg:
            return
        try:
            async with self._pg.acquire() as conn:
                await conn.execute(
                    """INSERT INTO user_memory (user_id, profile_summary, updated_at)
                       VALUES ($1, $2, NOW())
                       ON CONFLICT (user_id) DO UPDATE SET
                       profile_summary = EXCLUDED.profile_summary,
                       updated_at = NOW()""",
                    self._user_id, profile,
                )
        except Exception as e:
            logger.warning(f"Failed to persist profile_summary: {e}")
