# pigagent/context/storage/redis.py
"""Redis key patterns and I/O helpers for context module."""

from __future__ import annotations

import json

from loguru import logger

from config import get_config

_cfg = get_config()

from context.schema import ConversationRecord

_USER_TTL = 604800  # 7 days — all keys for a user share this TTL


async def _refresh_user_ttl(redis, user_id: str) -> None:
    """Reset TTL on all context keys for a user.

    Keeps the full context (turns, summaries, game_state) alive as a unit.
    Any write resets the TTL for ALL keys — prevents partial expiry where
    turns survive but summaries are gone.
    """
    if redis is None:
        return
    try:
        keys = [
            RedisKeys.turns(user_id),
            RedisKeys.summaries(user_id),
            RedisKeys.game_state(user_id),
        ]
        for key in keys:
            await redis.expire(key, _USER_TTL)
    except Exception:
        pass


class RedisKeys:
    """Canonical Redis key patterns. All keyed by user_id."""

    @staticmethod
    def turns(user_id: str) -> str:
        return f"ctx:{user_id}:turns"

    @staticmethod
    def compressing(user_id: str) -> str:
        return f"ctx:{user_id}:compressing"

    @staticmethod
    def summaries(user_id: str) -> str:
        return f"ctx:{user_id}:summaries"

    @staticmethod
    def game_state(user_id: str) -> str:
        return f"ctx:{user_id}:game_state"


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

    # ── Summaries (L2 + L3 + L4 in one key) ──────────────────────────

    async def read_summaries(self) -> dict:
        """Read all three layer summaries in one GET. Returns empty dict on miss."""
        if not self._redis:
            return {}
        try:
            raw = await self._redis.get(RedisKeys.summaries(self._user_id))
            if raw:
                return json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        except Exception:
            pass
        return {}

    async def write_summaries(
        self, end_turn: int, *,
        l2_profile: str = "", l3_session: str = "", l4_roast: str = "",
        roast_id: str = "", roast_prompt: str = "", roast_prompt_turn: int = 0,
    ) -> None:
        """Write all three layer summaries in one SET."""
        if not self._redis:
            return
        try:
            data = json.dumps({
                "end_turn": end_turn,
                "l2_profile": l2_profile,
                "l3_session": l3_session,
                "l4_roast": l4_roast,
                "roast_id": roast_id,
                "roast_prompt": roast_prompt,
                "roast_prompt_turn": roast_prompt_turn,
            }, ensure_ascii=False)
            await self._redis.set(RedisKeys.summaries(self._user_id), data, ex=_USER_TTL)
        except Exception:
            pass
        else:
            await _refresh_user_ttl(self._redis, self._user_id)

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

    # ── Turns Write ──────────────────────────────────────────────────

    async def push_turn(self, turn_data: str) -> None:
        if not self._redis:
            return
        try:
            async with self._redis.pipeline() as pipe:
                pipe.rpush(RedisKeys.turns(self._user_id), turn_data)
                pipe.ltrim(RedisKeys.turns(self._user_id), -_cfg.CONTEXT_HOT_WINDOW_SIZE, -1)
                await pipe.execute()
            await _refresh_user_ttl(self._redis, self._user_id)
        except Exception as e:
            logger.warning(f"Redis turn push failed: {e}")



