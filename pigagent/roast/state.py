"""RoastState — a single roast game session's mutable state."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from model import Mode, Phase

_ACTIVE_KEY = "roast:state:active:{user_id}"
_ACTIVE_TTL = 86400  # 24h


@dataclass
class RoastState:
    """Mutable state for one roast game session.

    Created via RoastState.start() — the only public constructor.
    Deserialized via RoastState.from_dict() — for loading from Redis/PG.
    """

    user_id: str
    persona_id: str
    news_id: str
    mode: Mode
    roast_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    phase: Phase = Phase.ACTIVE
    turn_count: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    # ── Serialization ────────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {
            "roast_id": self.roast_id,
            "user_id": self.user_id,
            "persona_id": self.persona_id,
            "news_id": self.news_id,
            "mode": str(self.mode),
            "phase": str(self.phase),
            "turn_count": self.turn_count,
            "extra": self.extra,
        }

    @classmethod
    def from_dict(cls, data: dict) -> RoastState:
        state = cls.__new__(cls)
        state.user_id = data["user_id"]
        state.persona_id = data.get("persona_id", "")
        state.news_id = data.get("news_id", "")
        state.mode = Mode(data["mode"]) if isinstance(data["mode"], str) else data["mode"]
        state.roast_id = data["roast_id"]
        state.phase = Phase(data["phase"]) if isinstance(data["phase"], str) else data["phase"]
        state.turn_count = data.get("turn_count", 0)
        state.extra = data.get("extra", {})
        return state

    # ── Factory (only public constructor) ────────────────────────────────

    @classmethod
    async def start(
        cls,
        user_id: str,
        persona_id: str,
        news_id: str,
        mode: Mode,
        *,
        redis,
        pg_pool=None,
    ) -> RoastState:
        """Start a new roast session. Auto-closes any previous active one."""
        prev = await cls._load_active(user_id, redis)
        if prev and prev.phase != Phase.CLOSED:
            prev.phase = Phase.CLOSED
            if pg_pool:
                await cls._save_history(prev, pg_pool)
            await cls._delete_active(user_id, redis)
            logger.info(f"[RoastState] Closed previous: {prev.roast_id}")

        state = cls.__new__(cls)
        state.user_id = user_id
        state.persona_id = persona_id
        state.news_id = news_id
        state.mode = mode
        state.roast_id = str(uuid.uuid4())
        state.phase = Phase.ACTIVE
        state.turn_count = 0
        state.extra = {}

        await state._save_active(redis)
        logger.info(f"[RoastState] Started: {state.roast_id} mode={mode.value}")
        return state

    # ── Persistence ──────────────────────────────────────────────────────

    async def save(self, redis, pg_pool=None) -> None:
        """Write current state to Redis, optionally PG."""
        await self._save_active(redis)
        if pg_pool:
            await self._save_history(self, pg_pool)

    async def close(self, redis, pg_pool=None) -> None:
        """Mark this session as closed and persist to history."""
        self.phase = Phase.CLOSED
        if pg_pool:
            await self._save_history(self, pg_pool)
        await self._delete_active(self.user_id, redis)

    # ── Internal storage helpers ─────────────────────────────────────────

    async def _save_active(self, redis) -> None:
        try:
            data = json.dumps(self.to_dict(), ensure_ascii=False)
            await redis.setex(
                _ACTIVE_KEY.format(user_id=self.user_id), _ACTIVE_TTL, data,
            )
        except Exception as e:
            logger.error(f"[RoastState] save_active failed: {e}")

    @classmethod
    async def _load_active(cls, user_id: str, redis) -> RoastState | None:
        try:
            data = await redis.get(_ACTIVE_KEY.format(user_id=user_id))
            if data:
                return cls.from_dict(json.loads(data))
        except Exception as e:
            logger.warning(f"[RoastState] load_active failed: {e}")
        return None

    @classmethod
    async def _delete_active(cls, user_id: str, redis) -> None:
        try:
            await redis.delete(_ACTIVE_KEY.format(user_id=user_id))
        except Exception as e:
            logger.warning(f"[RoastState] delete_active failed: {e}")

    @staticmethod
    async def _save_history(state: RoastState, pg_pool) -> None:
        try:
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO roast_states (roast_id, user_id, persona_id, news_id,
                        mode, phase, turn_count, extra)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    ON CONFLICT (roast_id) DO UPDATE SET
                        phase = EXCLUDED.phase,
                        turn_count = EXCLUDED.turn_count,
                        extra = EXCLUDED.extra
                    """,
                    state.roast_id,
                    state.user_id,
                    state.persona_id,
                    state.news_id,
                    str(state.mode),
                    str(state.phase),
                    state.turn_count,
                    json.dumps(state.extra, ensure_ascii=False),
                )
        except Exception as e:
            logger.error(f"[RoastState] save_history failed: {e}")
