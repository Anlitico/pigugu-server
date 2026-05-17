# agent/context/loader.py
"""ContextLoader — single entry point for agent context assembly.

Usage:
    loader = ContextLoader(redis_client=redis, pg_pool=pg)
    result = await loader.load(user_id="u1", roast_id="r123")
    resp = await llm.chat_stream(result.messages)
    await loader.record_turn(user_id="u1", role="assistant", content=resp_content)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from loguru import logger

from .manager import ContextManager
from .storage.redis import RedisKeys


@dataclass
class LoadResult:
    messages: list = field(default_factory=list)
    meta: dict = field(default_factory=dict)


class ContextLoader:
    """Single entry point for agent context assembly. One instance per app."""

    def __init__(self, *, redis_client=None, pg_pool=None, persona_prompt: str = ""):
        self._redis = redis_client
        self._pg = pg_pool
        self._persona_prompt = persona_prompt
        self._ctx = ContextManager(redis_client=redis_client, pg_pool=pg_pool)

    # ── Public ───────────────────────────────────────────────────────

    async def load(self, *, user_id: str, roast_id: str | None = None) -> LoadResult:
        """Assemble complete context for an LLM call."""
        wc = await self._ctx.assemble(user_id)

        messages = wc.to_messages(system_prompt=self._build_system_prompt())

        meta = {
            "user_id": user_id,
            "turn_count": int(wc.meta.get("turn_count", 0)),
            "tier": wc.tier,
            "roast_id": roast_id,
        }

        logger.debug(
            f"[Loader] load(user={user_id}, roast={roast_id}) "
            f"→ {len(messages)} msgs, tier={wc.tier}"
        )
        return LoadResult(messages=messages, meta=meta)

    async def record_turn(self, *, user_id: str, role: str, content: str) -> None:
        await self._ctx.add_turn(user_id, role, content)

    async def end_roast(self, *, user_id: str) -> None:
        await self._ctx.end_roast(user_id)

    async def write_game_state(self, *, user_id: str, state: dict) -> None:
        if self._redis:
            try:
                await self._redis.hset(
                    RedisKeys.game_state(user_id),
                    mapping={k: str(v) for k, v in state.items()},
                )
            except Exception:
                pass

    def _build_system_prompt(self) -> str:
        return self._persona_prompt
