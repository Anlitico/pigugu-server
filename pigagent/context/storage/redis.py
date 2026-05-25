# pigagent/context/storage/redis.py
"""Redis key patterns and I/O helpers for context module."""

from __future__ import annotations

import json

from loguru import logger

from config import get_config

_cfg = get_config()

from core.llm.types import Message
from context.schema import UserMemory, ConversationRecord, SummaryRecord

class RedisKeys:
    """Canonical Redis key patterns. All keyed by user_id."""

    @staticmethod
    def turns(user_id: str) -> str:
        return f"ctx:{user_id}:turns"

    @staticmethod
    def compressing(user_id: str) -> str:
        return f"ctx:{user_id}:compressing"

    @staticmethod
    def summary(user_id: str) -> str:
        return f"ctx:{user_id}:summary"

    @staticmethod
    def game_state(user_id: str) -> str:
        return f"ctx:{user_id}:game_state"

    @staticmethod
    def user_memory(user_id: str) -> str:
        return f"pigugu:user:{user_id}:memory"

    @staticmethod
    def roast_prompt(user_id: str) -> str:
        return f"ctx:{user_id}:roast:prompt"

    @staticmethod
    def roast_turns(user_id: str) -> str:
        return f"ctx:{user_id}:roast:turns"

    @staticmethod
    def roast_summary(user_id: str) -> str:
        return f"ctx:{user_id}:roast:summary"

    @staticmethod
    def roast_meta(user_id: str) -> str:
        return f"ctx:{user_id}:roast:meta"


class RedisStorage:
    """Redis read/write helpers. All keyed by user_id."""

    def __init__(self, user_id: str, redis_client=None):
        self._user_id = user_id
        self._redis = redis_client

    # ── Turns ───────────────────────────────────────────────────────

    async def get_hot_turns(self, n: int, *, after_anchor: int = 0) -> list[ConversationRecord]:
        """Return the last N turns (newest last), oldest -> newest order."""
        if not self._redis:
            return []
        try:
            read_count = max(n * 3, 30)
            raw = await self._redis.lrange(
                RedisKeys.turns(self._user_id), -read_count, -1
            )
            if not raw:
                return []

            records: list[ConversationRecord] = []
            for t in raw:
                d = json.loads(t.decode() if isinstance(t, bytes) else t)
                turn_num = d.get("turn", 0)
                if after_anchor == 0 or turn_num > after_anchor:
                    records.append(ConversationRecord.from_dict(d))

            # Keep the last N
            return records[-n:] if len(records) > n else records
        except Exception as e:
            logger.warning(f"Redis LRANGE failed: {e}")
            return []

    async def get_all_turns_with_numbers(self) -> list[ConversationRecord]:
        if not self._redis:
            return []
        try:
            raw = await self._redis.lrange(RedisKeys.turns(self._user_id), 0, -1)
            results = []
            for t in raw:
                d = json.loads(t.decode() if isinstance(t, bytes) else t)
                results.append(ConversationRecord.from_dict(d))
            return results
        except Exception:
            return []

    # ── Turn Counter ─────────────────────────────────────────────────

    async def get_last_turn_number(self) -> int:
        """Get turn_number from the most recent record. Returns 0 if no turns."""
        records = await self.get_hot_turns(1, after_anchor=0)
        return records[0].turn_number if records else 0

    async def has_turns(self) -> bool:
        if not self._redis:
            return False
        try:
            return await self._redis.exists(RedisKeys.turns(self._user_id)) > 0
        except Exception:
            return False

    # ── Compression Lock ─────────────────────────────────────────────

    async def is_compressing(self) -> bool:
        if not self._redis:
            return False
        try:
            raw = await self._redis.get(RedisKeys.compressing(self._user_id))
            return raw == b"1" if isinstance(raw, bytes) else raw == "1"
        except Exception:
            return False

    async def set_compressing(self, value: bool) -> None:
        if not self._redis:
            return
        try:
            if value:
                await self._redis.set(RedisKeys.compressing(self._user_id), "1", ex=300)
            else:
                await self._redis.delete(RedisKeys.compressing(self._user_id))
        except Exception:
            pass

    # ── Summaries ────────────────────────────────────────────────────

    async def read_summary(self) -> SummaryRecord | None:
        if not self._redis:
            return None
        try:
            raw = await self._redis.get(RedisKeys.summary(self._user_id))
            if raw:
                return SummaryRecord.deserialize(raw.decode() if isinstance(raw, bytes) else raw)
        except Exception:
            pass
        return None

    async def write_summary(self, sr: SummaryRecord) -> None:
        if not self._redis:
            return
        try:
            await self._redis.set(RedisKeys.summary(self._user_id), sr.serialize())
        except Exception:
            pass

    # ── Game State ───────────────────────────────────────────────────

    async def read_game_state(self) -> dict:
        if not self._redis:
            return {}
        try:
            raw = await self._redis.hgetall(RedisKeys.game_state(self._user_id))
            if raw:
                return {
                    (k.decode() if isinstance(k, bytes) else k):
                    (v.decode() if isinstance(v, bytes) else v)
                    for k, v in raw.items()
                }
        except Exception:
            pass
        return {}

    # ── User Memory ──────────────────────────────────────────────────

    async def load_user_memory(self) -> UserMemory | None:
        if not self._redis:
            return None
        try:
            raw = await self._redis.hgetall(RedisKeys.user_memory(self._user_id))
            if raw:
                h = {
                    (k.decode() if isinstance(k, bytes) else k):
                    (v.decode() if isinstance(v, bytes) else v)
                    for k, v in raw.items()
                }
                return UserMemory.from_hash(h)
        except Exception:
            pass
        return None

    async def write_user_memory(self, um: UserMemory) -> None:
        if not self._redis:
            return
        try:
            await self._redis.hset(
                RedisKeys.user_memory(self._user_id),
                mapping=um.to_hash(),
            )
        except Exception:
            pass

    # ── Turns Write ──────────────────────────────────────────────────

    async def push_turn(self, turn_data: str) -> None:
        if not self._redis:
            return
        try:
            async with self._redis.pipeline() as pipe:
                pipe.rpush(RedisKeys.turns(self._user_id), turn_data)
                pipe.ltrim(RedisKeys.turns(self._user_id), -_cfg.CONTEXT_HOT_WINDOW_SIZE, -1)
                await pipe.execute()
        except Exception as e:
            logger.warning(f"Redis turn push failed: {e}")

    # ── Roast ────────────────────────────────────────────────────────

    async def read_roast_prompt(self) -> str:
        if not self._redis:
            return ""
        try:
            raw = await self._redis.get(RedisKeys.roast_prompt(self._user_id))
            return raw.decode() if isinstance(raw, bytes) else raw if raw else ""
        except Exception:
            return ""

    async def read_roast_summary(self) -> str:
        if not self._redis:
            return ""
        try:
            raw = await self._redis.get(RedisKeys.roast_summary(self._user_id))
            return raw.decode() if isinstance(raw, bytes) else raw if raw else ""
        except Exception:
            return ""

    async def write_roast_summary(self, summary: str) -> None:
        if not self._redis:
            return
        try:
            await self._redis.set(RedisKeys.roast_summary(self._user_id), summary)
        except Exception:
            pass

    async def read_roast_meta(self) -> dict:
        if not self._redis:
            return {}
        try:
            raw = await self._redis.hgetall(RedisKeys.roast_meta(self._user_id))
            if raw:
                return {
                    (k.decode() if isinstance(k, bytes) else k):
                    (v.decode() if isinstance(v, bytes) else v)
                    for k, v in raw.items()
                }
        except Exception:
            pass
        return {}

    async def write_roast_meta(self, mapping: dict) -> None:
        if not self._redis:
            return
        try:
            await self._redis.hset(RedisKeys.roast_meta(self._user_id), mapping=mapping)
        except Exception:
            pass

    async def read_roast_turns_raw(self) -> list[bytes]:
        if not self._redis:
            return []
        try:
            raw = await self._redis.lrange(RedisKeys.roast_turns(self._user_id), 0, -1)
            return list(raw)
        except Exception:
            return []

    async def delete_roast_keys(self) -> None:
        if not self._redis:
            return
        try:
            await self._redis.delete(
                RedisKeys.roast_prompt(self._user_id),
                RedisKeys.roast_turns(self._user_id),
                RedisKeys.roast_summary(self._user_id),
                RedisKeys.roast_meta(self._user_id),
            )
        except Exception:
            pass

