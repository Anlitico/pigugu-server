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
from metrics.turn import TelemetryCollector

from agent_config import get_config

from .storage.memory import MemoryStore
from .storage.redis import RedisStorage, RedisKeys, _refresh_user_ttl, _USER_TTL
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
        self._turn_lock = asyncio.Lock()

    def _store(self, user_id: str) -> RedisStorage:
        return RedisStorage(user_id, self._redis)

    def _pg(self, user_id: str) -> PgStorage:
        return PgStorage(user_id, self._pg_pool)

    def _mem(self, user_id: str) -> MemoryStore:
        return MemoryStore(user_id)

    # ── Session Lifecycle ─────────────────────────────────────────────

    async def end_roast(self, user_id: str) -> None:
        logger.info(f"[Context] Roast ended user={user_id}")

    # ── Public Entry Points ───────────────────────────────────────────

    async def load(self, *, user_id: str) -> list:
        """Assemble context and return messages (no system prompt  -  caller injects)."""
        wc = await self.assemble(user_id)
        return wc.to_messages()

    async def write_game_state(self, *, user_id: str, state: dict) -> None:
        mem = self._mem(user_id)
        mem.write_game_state(state)
        if self._redis:
            try:
                asyncio.create_task(self._write_game_state_redis(user_id, state))
            except Exception:
                pass

    async def _write_game_state_redis(self, user_id: str, state: dict) -> None:
        redis = self._redis
        if redis is None:
            return
        await redis.hset(
            RedisKeys.game_state(user_id),
            mapping={k: str(v) for k, v in state.items()},
        )
        await redis.expire(RedisKeys.game_state(user_id), _USER_TTL)
        await _refresh_user_ttl(redis, user_id)

    # ── Turn Recording ────────────────────────────────────────────────

    async def add_turn(
        self, user_id: str, role: str, content: str, *,
        tool_calls: list | None = None,
        tool_call_id: str | None = None,
        name: str | None = None,
        partial: bool = False,
    ) -> int:
        mem = self._mem(user_id)
        store = self._store(user_id)
        pg = self._pg(user_id)

        # Serialise turn writes — prevents concurrent add_turn from
        # session.py events (e.g. interrupt) getting the same turn number.
        async with self._turn_lock:
            # Resolve turn number — memory first, then Redis, then PG
            current = mem.get_last_turn_number()
            if current == 0:
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
            # L1: write to memory immediately (synchronous, sub-ms)
            mem.push_turn(record)

        # Assign roast_instance_id synchronously — both in-memory and persisted
        # records need it for downstream filtering (Director, assemble).
        await self._assign_roast_instance_id(user_id, record)
            # Assign roast_instance_id synchronously — both in-memory and persisted
            # records need it for downstream filtering (Director, assemble).
            await self._assign_roast_instance_id(user_id, record)

            # L2 + L3: fire-and-forget to Redis and PG
            data = json.dumps(record.to_dict(), ensure_ascii=False)
            if self._redis:
                asyncio.create_task(self._bg_redis_push(store, data))
            if self._pg_pool:
                asyncio.create_task(pg.flush_one(turn_count, record.to_message(), record.roast_instance_id))

        return turn_count

    @staticmethod
    async def _bg_redis_push(store: RedisStorage, data: str) -> None:
        try:
            await store.push_turn(data)
        except Exception:
            pass

    async def _assign_roast_instance_id(self, user_id: str, current: ConversationRecord) -> None:
        mem = self._mem(user_id)
        history = mem.get_hot_turns(20)
        if not history:
            history = await self._store(user_id).get_hot_turns(20)
        RoastState.assign_roast_instance_id(history, current)

    # ── Context Assembly ──────────────────────────────────────────────

    async def assemble(self, user_id: str) -> WorkingContext:
        mem = self._mem(user_id)
        store = self._store(user_id)
        pg = self._pg(user_id)

        _cfg = get_config()

        # L1: read from memory
        data = mem.read_summaries()
        raw_records = mem.get_hot_turns(_cfg.CONTEXT_HOT_WINDOW_SIZE)
        source = "memory"
        TelemetryCollector.mark("ctx_l1_done")

        # L2: cold start — load from Redis into memory (only if both are empty)
        if not data and not raw_records:
            data = await store.read_summaries()
            if data:
                mem.write_summaries(data.get("end_turn", 0), **{k: v for k, v in data.items() if k != "end_turn"})
        if not raw_records:
            anchor = data.get("end_turn", 0)
            raw_records = await store.get_hot_turns(_cfg.CONTEXT_HOT_WINDOW_SIZE, after_anchor=anchor)
            if raw_records:
                mem.load_all(raw_records, data)
                source = "redis"
        TelemetryCollector.mark("ctx_l2_done")

        # L3: PG fallback when both memory and Redis are empty
        if not raw_records and pg._pg:
            row = await pg.read_latest_summary()
            if row:
                data = {
                    "end_turn": row.end_turn, "l2_profile": row.l2_profile,
                    "l3_session": row.l3_session, "l4_roast": row.l4_roast,
                    "roast_id": row.roast_id, "roast_prompt": row.roast_prompt,
                    "roast_prompt_turn": row.roast_prompt_turn,
                }
            else:
                profile_text, _ = await pg.read_profile()
                if profile_text:
                    data = {"end_turn": 0, "l2_profile": profile_text,
                            "l3_session": "", "l4_roast": "", "roast_id": "",
                            "roast_prompt": "", "roast_prompt_turn": 0}

            anchor = data.get("end_turn", 0)
            raw_records = await pg.recover_turns(after_turn=anchor, limit=_cfg.CONTEXT_HOT_WINDOW_SIZE)
            if raw_records:
                mem.load_all(raw_records, data)
                asyncio.create_task(self._rewarm_redis(user_id, data, raw_records))
                source = "pg"

        logger.info(
            f"[Context] assemble user={user_id} source={source} "
            f"records={len(raw_records)} summary={'yes' if data else 'no'}"
        )

        # Save summary text for compression trigger (before consuming)
        summary_for_compress = data.get("l3_session", "")

        # If summary exists (fresh from compressor): consume once, rebuild list, clear
        if data.get("l3_session"):
            raw_records = self._rebuild_records_with_summary(data, raw_records)
            mem.load_all(raw_records, {})
            # data is consumed — set empty so sr/um won't inject again
            data = {}

        sr = SummaryRecord(text=data["l3_session"], end_turn=data["end_turn"]) if data.get("l3_session") else None
        um = UserMemory(user_id=user_id, profile_summary=data.get("l2_profile", "")) if data.get("l2_profile") else None
        um = um or UserMemory(user_id=user_id)

        snap = ContextSnapshot(raw_records)

        game_state = mem.read_game_state()
        if not game_state:
            game_state = await store.read_game_state()
            if game_state:
                mem.write_game_state(game_state)

        wc = WorkingContext(
            user_id=user_id,
            game_state=game_state,
            meta={"turn_count": raw_records[0].turn_number if raw_records else 0},
            user_memory=um,
        )

        if sr:
            wc.summary = sr.text
            wc.summary_end_turn = sr.end_turn

        wc.raw_turns = [_record_to_msg(r) for r in raw_records]
        wc.raw_records = raw_records

        if snap.roast_instance_id:
            l4_fallback = data.get("l4_roast", "") if data.get("l4_roast") else None
            roast_prompt_fb = data.get("roast_prompt", "") if data.get("roast_prompt") else None
            prompt_turn = data.get("roast_prompt_turn", 0)
            wc.roast = self._load_roast_context(
                user_id, snap.roast_instance_id,
                fallback_l4=l4_fallback, fallback_prompt=roast_prompt_fb,
                prompt_turn=prompt_turn,
            )

        TelemetryCollector.mark("ctx_roast_done")

        # Compression trigger — fire-and-forget with unified record list
        if not mem.is_compressing() and raw_records:
            unified = wc.to_records()
            asyncio.create_task(
                self._compressor.run(
                    user_id=user_id,
                    records=unified,
                    existing_summary=summary_for_compress,
                )
            )

        return wc

    @staticmethod
    def _rebuild_records_with_summary(
        data: dict, records: list[ConversationRecord],
    ) -> list[ConversationRecord]:
        """Consume summary once: prepend L2/L3/L4 virtual records to the list.

        After this, the summaries dict is cleared — all layer data lives in the
        records list. Roast context (L4) is included as a virtual record with
        roast_instance_id set, so RoastState can reconstruct it from records alone.
        """
        new_records: list[ConversationRecord] = []
        if data.get("l2_profile"):
            new_records.append(ConversationRecord(
                turn_number=-3, role="system",
                content=f"[L2 Profile]\n{data['l2_profile']}",
                created_at=time.time(),
            ))
        if data.get("l3_session"):
            new_records.append(ConversationRecord(
                turn_number=-2, role="system",
                content=f"[L3 Session]\n{data['l3_session']}",
                created_at=time.time(),
            ))
        if data.get("l4_roast"):
            new_records.append(ConversationRecord(
                turn_number=-1, role="system",
                content=f"[L4 Roast]\n{data['l4_roast']}",
                roast_instance_id=data.get("roast_id", ""),
                created_at=time.time(),
            ))
        new_records.extend(records)
        return new_records

    # ── Layer 4: Roast Context ────────────────────────────────────────

    def _load_roast_context(
        self, user_id: str, roast_instance_id: str,
        fallback_l4: str | None = None,
        fallback_prompt: str | None = None,
        prompt_turn: int = 0,
    ) -> RoastContext:
        """Load roast context from records list first, summaries as fallback.

        After consume-once, L4 data lives as a virtual record (turn=-1) in the
        records list. The roast prompt is the first real record with this
        roast_instance_id.
        """
        mem = self._mem(user_id)
        rc = RoastContext(roast_instance_id=roast_instance_id)

        # L4 summary: from virtual record (turn=-1) with matching roast_instance_id
        records = mem.get_hot_turns(9999)
        for r in records:
            if r.turn_number == -1 and r.roast_instance_id == roast_instance_id:
                rc.summary = r.content.split("\n", 1)[1] if "\n" in r.content else r.content
                break
        if not rc.summary:
            rc.summary = fallback_l4 or ""

        # Roast prompt: from the first real record with this roast_instance_id
        for r in records:
            if r.turn_number > 0 and r.roast_instance_id == roast_instance_id:
                rc.prompt = r.content
                rc.prompt_turn = r.turn_number
                break
        if not rc.prompt:
            rc.prompt = fallback_prompt or ""
        if not rc.prompt_turn:
            rc.prompt_turn = prompt_turn

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
                roast_prompt=data.get("roast_prompt", ""),
                roast_prompt_turn=data.get("roast_prompt_turn", 0),
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
