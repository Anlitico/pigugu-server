# pigagent/context/storage/pg.py
"""PostgreSQL I/O for context module  -  turns, facts, profile, recovery."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import AsyncIterator

import asyncpg  # type: ignore[import-untyped]
from loguru import logger

from core.llm.types import Message
from context.schema import UserMemory, ConversationRecord, SummaryRow

# ── Global PG connection pool (lazy singleton) ──────────────────────────────

_pool: asyncpg.Pool | None = None
_pool_lock = asyncio.Lock()


async def _ensure_pg_pool() -> asyncpg.Pool:
    """Lazily create and return the global asyncpg connection pool."""
    global _pool
    if _pool is not None:
        return _pool

    async with _pool_lock:
        if _pool is not None:
            return _pool

        database_url = os.getenv("DATABASE_URL", "").replace("+asyncpg", "")
        if not database_url:
            raise RuntimeError(
                "DATABASE_URL is required. Set it in .env, "
                "e.g. postgresql://user:pass@localhost:5432/pigugu"
            )

        _pool = await asyncpg.create_pool(database_url, min_size=2, max_size=10)
        logger.info(f"[PG] Connection pool created (min=2, max=10)")
        return _pool


@asynccontextmanager
async def _connect(_dsn: str = "") -> AsyncIterator[asyncpg.pool.PoolConnectionProxy]:
    pool = await _ensure_pg_pool()
    async with pool.acquire() as conn:
        yield conn


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

    async def flush_one(self, turn_number: int, turn: Message, roast_instance_id: str | None) -> None:
        if not self._pg:
            return
        try:
            async with _connect(self._pg) as conn:
                await conn.execute(
                    """INSERT INTO agent_conversations
                       (user_id, turn_number, role, content,
                        tool_calls, tool_call_id, name, partial, roast_instance_id)
                       VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9)
                       ON CONFLICT (user_id, turn_number) DO NOTHING""",
                    self._user_id, turn_number,
                    turn.role, turn.content,
                    _serialize_tool_calls(turn.tool_calls),
                    turn.tool_call_id, turn.name, turn.partial,
                    roast_instance_id,
                )
        except Exception as e:
            if "Event loop is closed" not in str(e):
                logger.warning(f"PG flush_one failed: {e}")

    async def flush_buffer(self, batch: list[tuple[int, Message, str | None]]) -> None:
        if not self._pg:
            return
        try:
            async with _connect(self._pg) as conn:
                async with conn.transaction():
                    for turn_number, turn, roast_instance_id in batch:
                        await conn.execute(
                            """INSERT INTO agent_conversations
                               (user_id, turn_number, role, content,
                                tool_calls, tool_call_id, name, partial, roast_instance_id)
                               VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9)
                               ON CONFLICT (user_id, turn_number) DO NOTHING""",
                            self._user_id, turn_number,
                            turn.role, turn.content,
                            _serialize_tool_calls(turn.tool_calls),
                            turn.tool_call_id, turn.name, turn.partial,
                            roast_instance_id,
                        )
            logger.debug(f"Flushed {len(batch)} turns to PG")
        except Exception as e:
            if "Event loop is closed" not in str(e):
                logger.warning(f"PG flush failed: {e}")

    async def persist_turns(self, turns: list[tuple[int, Message, str | None]]) -> None:
        if not self._pg:
            return
        try:
            async with _connect(self._pg) as conn:
                async with conn.transaction():
                    for turn_number, turn, roast_instance_id in turns:
                        await conn.execute(
                            """INSERT INTO agent_conversations
                               (user_id, turn_number, role, content,
                                tool_calls, tool_call_id, name, partial, roast_instance_id)
                               VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9)
                               ON CONFLICT (user_id, turn_number) DO NOTHING""",
                            self._user_id, turn_number,
                            turn.role, turn.content,
                            _serialize_tool_calls(turn.tool_calls),
                            turn.tool_call_id, turn.name, turn.partial,
                            roast_instance_id,
                        )
            logger.info(f"Persisted {len(turns)} turns to PG")
        except Exception as e:
            logger.error(f"PG persist failed: {e}")

    async def recover_turn_counter(self) -> int:
        if not self._pg:
            return 0
        try:
            async with _connect(self._pg) as conn:
                row = await conn.fetchrow(
                    "SELECT COALESCE(MAX(turn_number), 0) FROM agent_conversations "
                    "WHERE user_id = $1",
                    self._user_id,
                )
                if row:
                    return row[0]
        except Exception:
            pass  # table not found — expected until migration runs
        return 0

    # ── Facts ──────────────────────────────────────────────────────

    async def persist_facts(self, fact_dicts: list[dict]) -> None:
        if not self._pg:
            return
        try:
            async with _connect(self._pg) as conn:
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
            async with _connect(self._pg) as conn:
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
            async with _connect(self._pg) as conn:
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
            async with _connect(self._pg) as conn:
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

    # ── Summaries ───────────────────────────────────────────────────

    async def write_summary_row(
        self, end_turn: int, *,
        l2_profile: str = "", l3_session: str = "", l4_roast: str = "",
        roast_id: str | None = None, roast_prompt: str = "", roast_prompt_turn: int = 0,
        model_used: str = "",
    ) -> None:
        if not self._pg:
            return
        try:
            async with _connect(self._pg) as conn:
                await conn.execute(
                    """INSERT INTO context_summaries
                       (user_id, end_turn, l2_profile, l3_session, l4_roast,
                        roast_id, roast_prompt, roast_prompt_turn, model_used)
                       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                       ON CONFLICT (user_id, end_turn) DO NOTHING""",
                    self._user_id, end_turn,
                    l2_profile, l3_session, l4_roast,
                    roast_id or "", roast_prompt, roast_prompt_turn, model_used,
                )
        except Exception as e:
            logger.warning(f"Failed to write summary row: {e}")

    async def read_latest_summary(self) -> SummaryRow | None:
        if not self._pg:
            return None
        try:
            async with _connect(self._pg) as conn:
                row = await conn.fetchrow(
                    """SELECT user_id, end_turn, l2_profile, l3_session, l4_roast,
                              roast_id, roast_prompt, roast_prompt_turn, model_used
                       FROM context_summaries
                       WHERE user_id = $1
                       ORDER BY end_turn DESC
                       LIMIT 1""",
                    self._user_id,
                )
                if row:
                    return SummaryRow(
                        user_id=row["user_id"],
                        end_turn=row["end_turn"],
                        l2_profile=row["l2_profile"],
                        l3_session=row["l3_session"],
                        l4_roast=row["l4_roast"],
                        roast_id=row["roast_id"],
                        roast_prompt=row["roast_prompt"],
                        roast_prompt_turn=row["roast_prompt_turn"] or 0,
                        model_used=row["model_used"],
                    )
        except Exception:
            pass
        return None

    # ── Turn Recovery ───────────────────────────────────────────────

    async def recover_turns(
        self, after_turn: int = 0, limit: int = 100,
    ) -> list[ConversationRecord]:
        if not self._pg:
            return []
        try:
            async with _connect(self._pg) as conn:
                rows = await conn.fetch(
                    """SELECT turn_number, role, content, tool_calls,
                              tool_call_id, name, partial, roast_instance_id, created_at
                       FROM agent_conversations
                       WHERE user_id = $1 AND turn_number > $2
                       ORDER BY turn_number
                       LIMIT $3""",
                    self._user_id, after_turn, limit,
                )
                records: list[ConversationRecord] = []
                for r in rows:
                    tcs_raw = r["tool_calls"]
                    tcs = None
                    if tcs_raw:
                        tcs = json.loads(tcs_raw) if isinstance(tcs_raw, str) else tcs_raw
                    created_at = r["created_at"]
                    ts = created_at.timestamp() if created_at else 0.0
                    records.append(ConversationRecord(
                        turn_number=r["turn_number"],
                        role=r["role"],
                        content=r["content"],
                        created_at=ts,
                        tool_calls=tcs,
                        tool_call_id=r["tool_call_id"],
                        name=r["name"],
                        partial=r["partial"] or False,
                        roast_instance_id=r["roast_instance_id"],
                    ))
                return records
        except Exception:
            pass  # table mismatch / no data — expected until migration runs
        return []
