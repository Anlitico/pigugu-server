"""PigAgent  -  pigugu voice agent: LLM orchestration, context, tools, roast.

Owns all content-level logic (system prompt, context assembly, tool execution,
game mode triggers). The voice bridge (lk.bridge) handles the LiveKit
pipeline adaptation  -  PigAgent itself has zero LiveKit dependency.

Public API:
    agent.generate_reply(user_id, user_text, persona_id)   -  high-level entry
    agent.stream(messages, persona_id)                     -  low-level ReAct loop
    agent.start_roast(user_id, persona_id, roast_id, mode_id)  -  begin roast game
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from loguru import logger

from core.llm.types import Message
from core.agent.runner import AgentRunner, RunnerConfig
from core.agent.stop import no_tool_calls
from context.manager import ContextManager
from roast.pending import consume
from roast.state import RoastState
from roast.types import Mode

_ROAST_USER_TAG = "[System  -  Game Background]"
_ROAST_TRIGGER_TAG = "[System]"


class PigAgent:
    """Pigugu agent  -  all LLM/content logic. Zero LiveKit dependency."""

    def __init__(
        self,
        ctx: ContextManager | None = None,
        *,
        redis,
        pg_pool,
        model: str = "qwen3.6-plus",
        prompts: dict[int, str] | None = None,
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
        self._prompts: dict[int, str] = prompts or {}
        self._game_modes: dict[str, Any] = game_modes or {}

        if tools is None:
            default_registry = self._create_default_tools()
            tools = default_registry.tools
            tool_handlers = default_registry.tool_handlers

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

    @staticmethod
    def _create_default_tools():
        """Create the default tool set: web_search + volume_control."""
        from tools import create_web_search_tool, volume_tool
        from tools.search import TavilyProvider
        from core.agent import ToolRegistry
        registry = ToolRegistry()
        registry.register(create_web_search_tool(TavilyProvider()))
        registry.register(volume_tool)
        return registry

    @property
    def model(self) -> str:
        return self._model

    # ── High-level entry point (called by bridge) ─────────────────────

    async def generate_reply(
        self,
        user_id: str,
        user_text: str,
        *,
        persona_id: int = 1,
    ) -> AsyncIterator[str]:
        """Complete reply pipeline: load context  ->  assemble  ->  stream  ->  persist.

        The single entry point for the voice bridge. Handles:
        - Context loading from Redis/PG
        - System prompt injection
        - Roast game routing (consume pending, inject body, tick)
        - Turn persistence after stream completes
        """
        if not user_text.strip():
            return

        # 1. Build new user message
        new_msg = Message.user(user_text.strip())

        # 2. Load context from Redis/PG
        messages: list[Message] = []
        if self.ctx and user_id:
            try:
                messages = await self.ctx.load(user_id=user_id)
            except Exception as e:
                logger.warning(f"[PigAgent] load context failed: {e}")

        messages.append(new_msg)

        # 3. Prepend system prompt
        prompt = self._prompts.get(persona_id, "")
        if prompt:
            messages.insert(0, Message.system(prompt))

        # 4. Check roast routing
        roast_state = await self.get_active_roast(user_id)
        game_mode = None
        if roast_state:
            mode_id = str(roast_state.mode) if hasattr(roast_state, "mode") else ""
            game_mode = self._game_modes.get(mode_id)

        # 5. Stream and collect response
        response_chunks: list[str] = []
        logger.info(
            f"[PigAgent] generate_reply user={user_id} persona={persona_id} "
            f"roast={roast_state is not None} msgs={len(messages)}"
        )

        if roast_state and game_mode:
            async for text in self._stream_roast(
                messages, roast_state, game_mode,
            ):
                response_chunks.append(text)
                yield text
        else:
            async for text in self.runner.stream(messages):
                response_chunks.append(text)
                yield text

        logger.info(
            f"[PigAgent] Reply complete: {self.runner.last_step_count} steps, "
            f"status={self.runner.last_status}"
        )

        # 6. Persist turn
        if self.ctx and user_id:
            full_response = "".join(response_chunks)
            await self._save_turn(user_id, new_msg.content, full_response)

    # ── Low-level stream (no side effects, used by tests) ──────────────

    async def stream(
        self,
        messages: list[Message],
        *,
        persona_id: int = 1,
        search: dict | None = None,
        interrupt_key: str | None = None,
    ) -> AsyncIterator[str]:
        """Low-level ReAct loop. No context loading, no persistence."""
        prompt = self._prompts.get(persona_id, "")
        if prompt:
            messages = [Message.system(prompt)] + messages

        async for text in self.runner.stream(
            messages, search=search, interrupt_key=interrupt_key,
        ):
            yield text

    # ── Roast ──────────────────────────────────────────────────────────

    async def start_roast(
        self,
        user_id: str,
        persona_id: int,
        roast_id: str,
        mode_id: str,
        prompt: str,
    ) -> AsyncIterator[str]:
        """Start a roast game and stream the opening reply.

        Called by the API service after resolving the scenario from PG.
        Creates the roast session, persists the roast body to context,
        then triggers generate_reply() to deliver the opening lines.
        Yields text chunks for TTS playback.
        """
        game_mode = self._game_modes.get(mode_id)
        if game_mode is None:
            logger.error(f"[PigAgent] Unknown game mode: {mode_id}")
            return

        state = await RoastState.start(
            user_id=user_id,
            persona_id=persona_id,
            roast_id=roast_id,
            mode=Mode(mode_id),
            extra=game_mode.init_extra() if hasattr(game_mode, "init_extra") else None,
            redis=self._redis,
            pg_pool=self._pg_pool,
        )

        # Persist roast body to context  -  loaded automatically on each turn
        roast_body = self._build_roast_body(
            game_mode=game_mode,
            prompt=prompt,
        )
        if roast_body and self.ctx:
            try:
                await self.ctx.add_turn(
                    user_id=user_id,
                    role="user",
                    content=f"{_ROAST_USER_TAG}\n{roast_body}",
                )
            except Exception as e:
                logger.error(f"[PigAgent] Failed to persist roast body: {e}")

        logger.info(
            f"[PigAgent] Roast started: {state.roast_instance_id} "
            f"roast_id={roast_id} mode={mode_id} user={user_id}"
        )

        # Trigger opening reply  -  roast body is already in context
        async for text in self.generate_reply(
            user_id, "Game start",
            persona_id=persona_id,
        ):
            yield text

    async def get_active_roast(self, user_id: str):
        try:
            return await RoastState._load_active(user_id, self._redis)
        except Exception as e:
            logger.warning(f"[PigAgent] get_active_roast failed: {e}")
            return None

    async def close_roast(self, user_id: str) -> None:
        try:
            state = await RoastState._load_active(user_id, self._redis)
            if state:
                await state.close(self._redis, self._pg_pool)
                logger.info(f"[PigAgent] Roast closed: {state.roast_instance_id}")
        except Exception as e:
            logger.error(f"[PigAgent] close_roast failed: {e}")

    # ── Internal ───────────────────────────────────────────────────────

    async def _stream_roast(
        self,
        messages: list[Message],
        roast_state,
        game_mode,
    ) -> AsyncIterator[str]:
        """Roast pipeline: consume pending  ->  stream  ->  tick.

        Roast body (news + game rules) was already persisted to context
        by start_roast()  -  it loads via ctx.load() in generate_reply().
        """
        # 1. Consume pending trigger prompt
        try:
            pending_prompt = await consume(roast_state.roast_instance_id, self._redis)
            if pending_prompt:
                messages.append(Message.user(
                    f"{_ROAST_TRIGGER_TAG}\n{pending_prompt}"
                ))
        except Exception as e:
            logger.warning(f"[PigAgent] consume_pending failed: {e}")

        # 2. Stream LLM
        async for text in self.runner.stream(messages):
            yield text

        # 3. Tick  -  fire-and-forget, don't block the reply
        asyncio.create_task(self._tick_roast(roast_state, game_mode, messages))

    async def _tick_roast(self, roast_state, game_mode, messages) -> None:
        """Background: advance state, check triggers, persist."""
        try:
            triggered = await game_mode.tick(
                roast_state, records=messages,
                redis=self._redis, pg_pool=self._pg_pool,
            )
            if triggered:
                logger.info(f"[PigAgent] Trigger fired: {triggered[:80]}...")
        except Exception as e:
            logger.error(f"[PigAgent] tick failed: {e}")

    async def _save_turn(
        self, user_id: str, user_content: str, assistant_content: str,
    ) -> None:
        """Persist one conversation turn to Redis/PG."""
        ctx = self._require_ctx()
        try:
            await ctx.add_turn(
                user_id=user_id, role="user", content=user_content,
            )
            if assistant_content:
                await ctx.add_turn(
                    user_id=user_id, role="assistant", content=assistant_content,
                )
        except Exception as e:
            logger.error(f"[PigAgent] save_turn failed: {e}")

    def _build_roast_body(self, *, game_mode, prompt: str = "") -> str:
        parts: list[str] = []
        if prompt.strip():
            parts.append(f"## News Context\n{prompt.strip()}")
        ext = getattr(game_mode, "system_prompt_extension", "")
        if ext:
            parts.append(f"## Game Mode\n{ext}")
        return "\n\n".join(parts)

    def _require_ctx(self) -> ContextManager:
        if self.ctx is None:
            raise RuntimeError("PigAgent created without ContextManager")
        return self.ctx
