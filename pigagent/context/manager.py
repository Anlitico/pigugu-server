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

from config import get_config

from .storage.redis import RedisStorage, RedisKeys
from .storage.pg import PgStorage
from .schema import UserMemory, RoastContext, WorkingContext, ConversationRecord, SummaryRecord
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
    ) -> int:
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
        return turn_count

    async def _assign_roast_instance_id(self, user_id: str, current: ConversationRecord) -> None:
        history = await self._store(user_id).get_hot_turns(20)
        RoastState.assign_roast_instance_id(history, current)

    # ── Context Assembly ──────────────────────────────────────────────

    async def assemble(self, user_id: str) -> WorkingContext:
        store = self._store(user_id)
        pg = self._pg(user_id)

        # Read all three layer summaries in one GET
        data = await store.read_summaries()
        sr = SummaryRecord(text=data["l3_session"], end_turn=data["end_turn"]) if data.get("l3_session") else None
        anchor = sr.end_turn if sr else 0

        _cfg = get_config()
        # Load with buffer — compression triggers at MAX_TURNS, but we need to
        # load enough to detect the threshold crossing.
        raw_records = await store.get_hot_turns(_cfg.CONTEXT_HOT_WINDOW_SIZE, after_anchor=anchor)
        um = UserMemory(user_id=user_id, profile_summary=data.get("l2_profile", "")) if data.get("l2_profile") else None

        # PG fallback when Redis is empty for this user
        if not raw_records and pg._pg:
            row = await pg.read_latest_summary()
            if row:
                data = {
                    "end_turn": row.end_turn, "l2_profile": row.l2_profile,
                    "l3_session": row.l3_session, "l4_roast": row.l4_roast,
                    "roast_id": row.roast_id,
                }
                sr = SummaryRecord(text=row.l3_session, end_turn=row.end_turn)
                anchor = row.end_turn
                um = UserMemory(user_id=user_id, profile_summary=row.l2_profile)
            else:
                profile_text, _ = await pg.read_profile()
                if profile_text:
                    um = UserMemory(user_id=user_id, profile_summary=profile_text)
                    data = {"end_turn": 0, "l2_profile": profile_text,
                            "l3_session": "", "l4_roast": "", "roast_id": ""}

            raw_records = await pg.recover_turns(after_turn=anchor, limit=_cfg.CONTEXT_HOT_WINDOW_SIZE)
            if raw_records:
                um = um or UserMemory(user_id=user_id)
                asyncio.create_task(self._rewarm_redis(user_id, data, raw_records))

        um = um or UserMemory(user_id=user_id)
        snap = ContextSnapshot(raw_records)

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
            l4_fallback = data.get("l4_roast", "") if data.get("l4_roast") else None
            wc.roast = await self._load_roast_context(
                user_id, snap.roast_instance_id, fallback_l4=l4_fallback,
            )

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

    async def _load_roast_context(
        self, user_id: str, roast_instance_id: str, fallback_l4: str | None = None,
    ) -> RoastContext:
        store = self._store(user_id)
        rc = RoastContext(roast_instance_id=roast_instance_id)
        try:
            rc.prompt = await store.read_roast_prompt()
            data = await store.read_summaries()
            rc.summary = data.get("l4_roast", "") or fallback_l4 or ""
        except Exception as e:
            logger.warning(f"Failed to load roast context for {user_id}: {e}")
        return rc

    # ── Redis Re-warming ───────────────────────────────────────────────

    async def _rewarm_redis(
        self, user_id: str,
        data: dict,
        records: list[ConversationRecord],
    ) -> None:
        """Re-populate Redis from PG-recovered data. Fire-and-forget."""
        store = self._store(user_id)
        try:
            await store.write_summaries(
                data.get("end_turn", 0),
                l2_profile=data.get("l2_profile", ""),
                l3_session=data.get("l3_session", ""),
                l4_roast=data.get("l4_roast", ""),
                roast_id=data.get("roast_id", ""),
            )
            for r in records:
                await store.push_turn(json.dumps(r.to_dict(), ensure_ascii=False))
            logger.info(f"[Context] Re-warmed Redis for user={user_id}: "
                        f"turns={len(records)}")
        except Exception as e:
            logger.warning(f"[Context] Re-warm Redis failed for {user_id}: {e}")


# ── Helpers ──────────────────────────────────────────────────────────────

def _record_to_msg(r: ConversationRecord):
    return r.to_message()
