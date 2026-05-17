# agent/context/manager.py
"""ContextManager — global orchestrator. Data-driven, no meta hash.

All position info is embedded in the data:
  - turn_count → last ConversationRecord.turn_number
  - roast_id   → ConversationRecord.roast_id
  - roast_start → ConversationRecord.roast_start
  - anchor     → SummaryRecord.end_turn
  - compressing → ctx:u1:compressing (independent key)
"""

from __future__ import annotations

import asyncio
import json

from loguru import logger

from config import get_config

_cfg = get_config()

from .storage.redis import RedisStorage
from .storage.pg import PgStorage
from .schema import UserMemory, RoastContext, WorkingContext, ConversationRecord
from .compression.compressor import ContextCompressor


class ContextManager:
    """Global context orchestrator. One instance for the entire app."""

    def __init__(self, *, redis_client=None, pg_pool=None):
        self._redis = redis_client
        self._pg_pool = pg_pool

    def _store(self, user_id: str) -> RedisStorage:
        return RedisStorage(user_id, self._redis)

    def _pg(self, user_id: str) -> PgStorage:
        return PgStorage(user_id, self._pg_pool)

    # ── Session Lifecycle ─────────────────────────────────────────────

    async def end_roast(self, user_id: str) -> None:
        try:
            await self._store(user_id).delete_roast_keys()
            logger.info(f"[Context] Roast ended user={user_id}")
        except Exception as e:
            logger.error(f"[Context] End roast failed for {user_id}: {e}")

    # ── Turn Recording ────────────────────────────────────────────────

    async def add_turn(self, user_id: str, role: str, content: str) -> None:
        store = self._store(user_id)
        pg = self._pg(user_id)

        current = await store.get_last_turn_number()
        if current == 0:
            current = await pg.recover_turn_counter()

        turn_count = current + 1
        record = ConversationRecord(
            turn_number=turn_count, role=role, content=content,
            roast_id=await self._detect_roast_id(user_id),
        )

        data = json.dumps(record.to_dict(), ensure_ascii=False)
        await store.push_turn(data)

        if self._pg_pool:
            asyncio.create_task(pg.flush_one(turn_count, record.to_message(), record.roast_id))

    async def _detect_roast_id(self, user_id: str) -> str | None:
        records = await self._store(user_id).get_hot_turns(1)
        if records and records[0].roast_id:
            return records[0].roast_id
        return None

    # ── Context Assembly ──────────────────────────────────────────────

    async def assemble(self, user_id: str) -> WorkingContext:
        store = self._store(user_id)

        # Read L3 summary (recursive, single)
        sr = await store.read_summary()
        anchor = sr.end_turn if sr else 0

        # Read raw turns after anchor
        raw_records = await store.get_hot_turns(20, after_anchor=anchor)
        roast_id = _find_active_roast(raw_records)

        um = await store.load_user_memory() or UserMemory(user_id=user_id)

        wc = WorkingContext(
            user_id=user_id,
            game_state=await store.read_game_state(),
            meta={"turn_count": raw_records[0].turn_number if raw_records else 0},
            user_memory=um,
        )

        if sr:
            wc.summary = sr.text
            wc.summary_end_turn = sr.end_turn

        raw_turns_to_show = [_record_to_msg(r) for r in reversed(
            raw_records[:_cfg.CONTEXT_RAW_TURN_COUNT]
        )]
        wc.raw_turns = raw_turns_to_show

        if roast_id:
            wc.roast = await self._load_roast_context(user_id, roast_id)

        # Compression trigger — delegate to ContextCompressor
        if not await store.is_compressing():
            all_records = await store.get_all_turns_with_numbers()
            asyncio.create_task(
                ContextCompressor(redis_store=store, pg_store=self._pg(user_id)).run(
                    user_id=user_id,
                    records=all_records,
                    existing_summary=sr.text if sr else "",
                )
            )

        return wc

    # ── Layer 4: Roast Context ────────────────────────────────────────

    async def _load_roast_context(self, user_id: str, roast_id: str) -> RoastContext:
        store = self._store(user_id)
        rc = RoastContext(roast_id=roast_id)
        try:
            rc.prompt = await store.read_roast_prompt()
            rc.summary = await store.read_roast_summary()
        except Exception as e:
            logger.warning(f"Failed to load roast context for {user_id}: {e}")
        return rc


# ── Helpers ──────────────────────────────────────────────────────────────

def _record_to_msg(r: ConversationRecord):
    from core.llm.types import Message
    return Message(role=r.role, content=r.content)


def _find_active_roast(records: list[ConversationRecord]) -> str:
    for r in records:
        if r.roast_id and not r.roast_start:
            return r.roast_id
    return ""


def _find_roast_start(records: list[ConversationRecord]) -> int:
    for r in records:
        if r.roast_start:
            return r.turn_number
    return 0
