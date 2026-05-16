# agent/context/loader.py
"""ContextLoader — single entry point for agent context assembly.

One instance for the entire app. Context is keyed by user_id only.
roast_id bundles mode + news + persona + mood — all resolved from
Redis meta at build time.

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


@dataclass
class LoadResult:
    """Output of ContextLoader.load(). Ready for LLM."""
    messages: list = field(default_factory=list)
    meta: dict = field(default_factory=dict)


class ContextLoader:
    """Stateless entry point for agent context assembly."""

    def __init__(self, *, redis_client=None, pg_pool=None):
        self._redis = redis_client
        self._pg = pg_pool

    # ── Public ───────────────────────────────────────────────────────

    async def load(
        self,
        *,
        user_id: str,
        roast_id: str | None = None,
    ) -> LoadResult:
        """Assemble complete context for an LLM call.

        If roast_id is empty, skips game mode / persona / news assembly.
        Only conversation history and user memory are injected.
        """
        ctx = self._make_ctx(user_id)

        if roast_id and not await self._session_exists(user_id):
            await ctx.init_session(roast_id)

        system_prompt = await self._build_system_prompt(ctx)

        wc = await ctx.assemble()

        # Collect summaries of closed segments (for cross-roast context)
        prev_summaries = await ctx._get_closed_segment_summaries()

        messages = wc.to_messages(
            system_prompt=system_prompt,
            user_global_summary=ctx.user_memory.global_summary,
            previous_segments=prev_summaries,
        )

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

    async def record_turn(
        self, *, user_id: str, role: str, content: str,
    ) -> None:
        ctx = self._make_ctx(user_id)
        await ctx.add_turn(role, content)

    async def end_roast(self, *, user_id: str) -> None:
        """End the current roast and clean up roast state."""
        ctx = self._make_ctx(user_id)
        await ctx.end_roast()

    async def write_game_state(
        self, *, user_id: str, state: dict,
    ) -> None:
        if self._redis:
            try:
                from .schemas import RedisKeys
                await self._redis.hset(
                    RedisKeys.game_state(user_id),
                    mapping={k: str(v) for k, v in state.items()},
                )
            except Exception:
                pass

    # ── Internal ──────────────────────────────────────────────────────

    def _make_ctx(self, user_id: str) -> ContextManager:
        return ContextManager(
            user_id=user_id,
            redis_client=self._redis,
            pg_pool=self._pg,
        )

    async def _session_exists(self, user_id: str) -> bool:
        if not self._redis:
            return False
        try:
            from .schemas import RedisKeys
            return await self._redis.exists(RedisKeys.meta(user_id)) > 0
        except Exception:
            return False

    async def _build_system_prompt(self, ctx: ContextManager) -> str:
        """Assemble layered system prompt from Redis meta."""
        parts: list[str] = []

        meta = await ctx._read_meta()
        persona_id = meta.get("persona_id", "")
        mode_id = meta.get("mode_id", "")
        news_id = meta.get("news_id", "")
        mood = meta.get("mood", "")

        persona = self._get_persona(persona_id)

        if persona:
            parts.append(persona.personality_prompt)

        if mood:
            parts.append(mood)
        else:
            parts.append("Mood: Default (dry sarcasm)")

        if news_id:
            parts.append(f"News ID: {news_id}")

        game_mode = self._get_game_mode(mode_id) if mode_id else None
        if game_mode:
            parts.append(
                f"Game Mode: {game_mode.display_name} ({game_mode.mode_id})"
            )
            ext = game_mode.system_prompt_extension
            if ext:
                parts.append(ext)

        turn_count = int(meta.get("turn_count", 0))
        max_turns = game_mode.get_max_turns() if game_mode else 10
        parts.append(
            f"## CURRENT TURN\n"
            f"This is turn {turn_count + 1} of the conversation. "
            f"Max turns: {max_turns}."
        )

        return "\n\n".join(filter(None, parts))

    @staticmethod
    def _get_persona(persona_id: str):
        if not persona_id:
            return None
        try:
            from personas import PersonaRegistry
            return PersonaRegistry.get(persona_id)
        except Exception:
            return None

    @staticmethod
    def _get_game_mode(mode_id: str):
        if not mode_id:
            return None
        try:
            from roasts import GameModeRegistry
            return GameModeRegistry.get(mode_id)
        except Exception:
            return None
