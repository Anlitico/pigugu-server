"""PigAgent — pigugu voice agent: LLM orchestration, context, tools, roast.

Owns all content-level logic (system prompt, context assembly, tool execution,
game mode triggers). The voice bridge (lk.bridge) handles the LiveKit
pipeline integration — PigAgent itself has zero LiveKit dependency.

Public API:
    PigAgent(model=...)         — create an agent
    agent.stream(messages)      — normal chat ReAct loop
    agent.stream_roast(messages)— roast chat with consume/tick pipeline
    agent.start_roast(...)      — begin a roast game session
    agent.run(user_id=...)      — context-managed standalone run
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from loguru import logger

from core.llm.types import Message
from core.agent.runner import AgentRunner, RunnerConfig
from core.agent.stop import StepResult, no_tool_calls
from context.manager import ContextManager

_ROAST_USER_TAG = "[系统提示 — 游戏背景]"
_ROAST_TRIGGER_TAG = "[系统提示]"


class PigAgent:
    """Pigugu agent — LLM orchestration and content logic.

    Zero LiveKit dependency. The bridge (lk.bridge.PigAgentVoiceBridge)
    handles all voice pipeline integration.
    """

    def __init__(
        self,
        ctx: ContextManager | None = None,
        *,
        redis,
        pg_pool,
        model: str = "qwen3.6-plus",
        prompts: dict[str, str] | None = None,
        game_modes: dict[str, Any] | None = None,
        tools: list | None = None,
        tool_handlers: dict | None = None,
        temperature: float = 0.6,
        max_tokens: int | None = None,
        max_iterations: int = 5,
        tool_timeout: float = 60.0,
        interrupt_key: str | None = None,
    ):
        self.ctx = ctx
        self._redis = redis
        self._pg_pool = pg_pool
        self._prompts: dict[str, str] = prompts or {}
        self._game_modes: dict[str, Any] = game_modes or {}

        runner_config = RunnerConfig(
            model=model,
            tools=tools or [],
            tool_handlers=tool_handlers or {},
            tool_timeout=tool_timeout,
            max_steps=max_iterations,
            stop_when=[no_tool_calls],
            temperature=temperature,
            max_tokens=max_tokens,
            interrupt_key=interrupt_key,
        )
        self._model = model
        self.runner = AgentRunner(runner_config)

    @property
    def model(self) -> str:
        return self._model

    # ── Normal chat ────────────────────────────────────────────────────

    async def stream(
        self,
        messages: list[Message],
        *,
        persona_id: str = "",
        search: dict | None = None,
        interrupt_key: str | None = None,
    ) -> AsyncIterator[str]:
        """Stream the ReAct loop, prepending the persona system prompt."""
        prompt = self._prompts.get(persona_id, "")
        if prompt:
            messages = [Message.system(prompt)] + messages

        logger.info(
            f"[PigAgent] Stream, persona={persona_id}, "
            f"interrupt_key={interrupt_key}"
        )

        async for text in self.runner.stream(
            messages, search=search, interrupt_key=interrupt_key,
        ):
            yield text

        logger.info(
            f"[PigAgent] Stream complete: {self.runner.last_step_count} steps, "
            f"status={self.runner.last_status}"
        )

    # ── Roast chat ─────────────────────────────────────────────────────

    async def start_roast(
        self,
        user_id: str,
        persona_id: str,
        news_id: str,
        mode_id: str,
        *,
        news_content: str = "",
    ) -> dict | None:
        """Start a roast game session. Returns state metadata dict for callers."""
        game_mode = self._game_modes.get(mode_id)
        if game_mode is None:
            logger.error(f"[PigAgent] Unknown game mode: {mode_id}")
            return None

        from roast.state import RoastState
        from roast.types import Mode

        state = await RoastState.start(
            user_id=user_id,
            persona_id=persona_id,
            news_id=news_id,
            mode=Mode(mode_id),
            extra=game_mode.init_extra() if hasattr(game_mode, "init_extra") else None,
            redis=self._redis,
            pg_pool=self._pg_pool,
        )

        # Build roast body for first-turn injection
        state.extra["_roast_body"] = self._build_roast_body(
            game_mode=game_mode,
            news_content=news_content,
        )

        logger.info(
            f"[PigAgent] Roast started: {state.roast_id} "
            f"mode={mode_id} user={user_id}"
        )
        return state.to_dict()

    async def stream_roast(
        self,
        messages: list[Message],
        *,
        persona_id: str = "",
        roast_state,
        search: dict | None = None,
        interrupt_key: str | None = None,
    ) -> AsyncIterator[str]:
        """Roast pipeline: consume pending → inject context → stream → tick.

        Wraps the normal stream() with roast-specific pre/post processing.
        roast_state is a RoastState instance (from start_roast or loaded from Redis).
        """
        # 0. Resolve game mode from state
        mode_id = str(roast_state.mode) if hasattr(roast_state, "mode") else ""
        game_mode = self._game_modes.get(mode_id)

        # 1. Consume pending trigger prompt
        try:
            from roast.pending import consume
            pending_prompt = await consume(roast_state.roast_id, self._redis)
            if pending_prompt:
                messages.append(Message.user(
                    f"{_ROAST_TRIGGER_TAG}\n{pending_prompt}"
                ))
                logger.info(f"[PigAgent] Injected pending prompt for {roast_state.roast_id}")
        except Exception as e:
            logger.warning(f"[PigAgent] consume_pending failed: {e}")

        # 2. First turn: inject roast body (game background)
        if roast_state.turn_count == 0:
            roast_body = roast_state.extra.get("_roast_body", "")
            if roast_body:
                messages.append(Message.user(
                    f"{_ROAST_USER_TAG}\n{roast_body}"
                ))
                logger.info(f"[PigAgent] Injected roast body (first turn)")

        # 3. Normal LLM stream
        async for text in self.stream(
            messages, persona_id=persona_id, search=search,
            interrupt_key=interrupt_key,
        ):
            yield text

        # 4. Tick — advance state, check triggers
        if game_mode:
            try:
                records = messages
                triggered = await game_mode.tick(
                    roast_state, records=records,
                    redis=self._redis, pg_pool=self._pg_pool,
                )
                if triggered:
                    logger.info(f"[PigAgent] Trigger fired: {triggered[:80]}...")
            except Exception as e:
                logger.error(f"[PigAgent] tick failed: {e}")

    async def get_active_roast(self, user_id: str):
        """Return active RoastState for user, or None."""
        try:
            from roast.state import RoastState
            return await RoastState._load_active(user_id, self._redis)
        except Exception as e:
            logger.warning(f"[PigAgent] get_active_roast failed: {e}")
            return None

    async def close_roast(self, user_id: str) -> None:
        """Close the active roast session for user."""
        try:
            from roast.state import RoastState
            state = await RoastState._load_active(user_id, self._redis)
            if state:
                await state.close(self._redis, self._pg_pool)
                logger.info(f"[PigAgent] Roast closed: {state.roast_id}")
        except Exception as e:
            logger.error(f"[PigAgent] close_roast failed: {e}")

    def _build_roast_body(self, *, game_mode, news_content: str = "") -> str:
        """Assemble the roast body for first-turn injection."""
        parts: list[str] = []
        if news_content.strip():
            parts.append(f"## 新闻背景\n{news_content.strip()}")
        ext = getattr(game_mode, "system_prompt_extension", "")
        if ext:
            parts.append(f"## 游戏模式\n{ext}")
        return "\n\n".join(parts)

    # ── Context-managed run (standalone, non-LiveKit) ─────────────────

    async def run(
        self, *, user_id: str, persona_id: str = "",
        interrupt_key: str | None = None,
    ) -> StepResult:
        """Run the agent loop with context from Redis/PG (non-streaming)."""
        effective_key = interrupt_key
        self.runner._interrupt_key = effective_key
        ctx = self._require_ctx()
        prompt = self._prompts.get(persona_id, "")
        initial_count = 0

        async def _load_context() -> list[Message]:
            nonlocal initial_count
            context = await ctx.load(user_id=user_id)
            if prompt:
                context.insert(0, Message.system(prompt))
            initial_count = len(context)
            return context

        async def _flush_all(messages: list, state) -> None:
            for msg in messages[initial_count:]:
                tool_calls = None
                if msg.tool_calls:
                    tool_calls = [
                        {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                        for tc in msg.tool_calls
                    ]
                await ctx.add_turn(
                    user_id=user_id, role=msg.role, content=msg.content,
                    tool_calls=tool_calls, tool_call_id=msg.tool_call_id,
                    name=msg.name,
                )

        logger.info(f"[PigAgent] Starting run for user={user_id}")
        result = await self.runner.run(
            on_before_step=_load_context, on_after_step=_flush_all,
        )
        logger.info(
            f"[PigAgent] Run complete: {self.runner.last_step_count} steps, "
            f"status={self.runner.last_status}"
        )
        return result

    # ── Context helpers ────────────────────────────────────────────────

    def _require_ctx(self) -> ContextManager:
        if self.ctx is None:
            raise RuntimeError("PigAgent created without ContextManager")
        return self.ctx

    async def load(self, *, user_id: str) -> list[Message]:
        return await self._require_ctx().load(user_id=user_id)

    async def add_turn(
        self, user_id: str, role: str, content: str, *,
        tool_calls: list | None = None, tool_call_id: str | None = None,
        name: str | None = None, partial: bool = False,
    ) -> None:
        await self._require_ctx().add_turn(
            user_id=user_id, role=role, content=content,
            tool_calls=tool_calls, tool_call_id=tool_call_id,
            name=name, partial=partial,
        )

    async def write_game_state(self, *, user_id: str, state: dict) -> None:
        await self._require_ctx().write_game_state(user_id=user_id, state=state)
