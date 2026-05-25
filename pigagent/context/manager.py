# pigagent/context/manager.py
"""ContextManager  -  global orchestrator. Data-driven, no meta hash.

All position info is embedded in the data:
  - turn_count  ->  last ConversationRecord.turn_number
  - roast_instance_id    ->  ConversationRecord.roast_instance_id
  - roast start/end  ->  inferred from roast_instance_id transitions
  - anchor      ->  SummaryRecord.end_turn
  - compressing  ->  ctx:u1:compressing (independent key)
"""

from __future__ import annotations

import asyncio
import json
import time

from loguru import logger

from .storage.redis import RedisStorage, RedisKeys
from .storage.pg import PgStorage
from .schema import UserMemory, RoastContext, WorkingContext, ConversationRecord
from .snapshot import ContextSnapshot
from .compression.compressor import ContextCompressor
from .roast import RoastState


class ContextManager:
    """Global context orchestrator. One instance for the entire app."""

    def __init__(self, *, redis_client=None, pg_pool=None):
        self._redis = redis_client
        self._pg_pool = pg_pool
        self._compressor = ContextCompressor(redis_client=redis_client, pg_pool=pg_pool)

    def _store(self, user_id: str) -> RedisStorage:
        return RedisStorage(user_id, self._redis)

    def _pg(self, user_id: str) -> PgStorage:
        return PgStorage(user_id, self._pg_pool)

    # ── Session Lifecycle ─────────────────────────────────────────────

    async def end_roast(self, user_id: str) -> None:
        logger.info(f"[Context] Roast ended user={user_id}")

    # ── Public Entry Points ───────────────────────────────────────────

    async def load(self, *, user_id: str) -> list:
        """Assemble context and return messages (no system prompt  -  caller injects)."""
        wc = await self.assemble(user_id)
        return wc.to_messages()

    async def write_game_state(self, *, user_id: str, state: dict) -> None:
        if not self._redis:
            return
        try:
            await self._redis.hset(
                RedisKeys.game_state(user_id),
                mapping={k: str(v) for k, v in state.items()},
            )
        except Exception:
            pass

    # ── Turn Recording ────────────────────────────────────────────────

    async def add_turn(
        self, user_id: str, role: str, content: str, *,
        tool_calls: list | None = None,
        tool_call_id: str | None = None,
        name: str | None = None,
        partial: bool = False,
    ) -> None:
        store = self._store(user_id)
        pg = self._pg(user_id)

        current = await store.get_last_turn_number()
        if current == 0:
            current = await pg.recover_turn_counter()

        turn_count = current + 1
        record = ConversationRecord(
            turn_number=turn_count, role=role, content=content,
            created_at=time.time(),
            tool_calls=tool_calls, tool_call_id=tool_call_id,
            name=name, partial=partial,
        )
        await self._assign_roast_instance_id(user_id, record)

        data = json.dumps(record.to_dict(), ensure_ascii=False)
        await store.push_turn(data)

        if self._pg_pool:
            asyncio.create_task(pg.flush_one(turn_count, record.to_message(), record.roast_instance_id))

    async def _assign_roast_instance_id(self, user_id: str, current: ConversationRecord) -> None:
        history = await self._store(user_id).get_hot_turns(20)
        RoastState.assign_roast_instance_id(history, current)

    # ── Context Assembly ──────────────────────────────────────────────

    async def assemble(self, user_id: str) -> WorkingContext:
        store = self._store(user_id)

        # Read L3 summary (recursive, single)
        sr = await store.read_summary()
        anchor = sr.end_turn if sr else 0

        # Read raw turns after anchor (at most 100, not yet compressed)
        raw_records = await store.get_hot_turns(100, after_anchor=anchor)
        snap = ContextSnapshot(raw_records)

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

        wc.raw_turns = [_record_to_msg(r) for r in raw_records]

        if snap.roast_instance_id:
            wc.roast = await self._load_roast_context(user_id, snap.roast_instance_id)

        # Compression trigger  -  same records, fire-and-forget
        if not await store.is_compressing() and raw_records:
            asyncio.create_task(
                self._compressor.run(
                    user_id=user_id,
                    records=raw_records,
                    existing_summary=sr.text if sr else "",
                )
            )

        return wc

    # ── Layer 4: Roast Context ────────────────────────────────────────

    async def _load_roast_context(self, user_id: str, roast_instance_id: str) -> RoastContext:
        store = self._store(user_id)
        rc = RoastContext(roast_instance_id=roast_instance_id)
        try:
            rc.prompt = await store.read_roast_prompt()
            rc.summary = await store.read_roast_summary()
        except Exception as e:
            logger.warning(f"Failed to load roast context for {user_id}: {e}")
        return rc


# ── Helpers ──────────────────────────────────────────────────────────────

def _record_to_msg(r: ConversationRecord):
    return r.to_message()
