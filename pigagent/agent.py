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
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from livekit.agents.types import FlushSentinel

from loguru import logger
from metrics.turn import TelemetryCollector

from core.llm.types import Message
from core.agent.runner import AgentRunner, RunnerConfig
from core.agent.stop import no_tool_calls
from context.manager import ContextManager
from roast.pending import consume
from roast.types import Phase
from roast.state import RoastState
from tools.roast import _current_user_id, _current_persona_id


class PigAgent:
    """Pigugu agent  -  all LLM/content logic. Zero LiveKit dependency."""

    def __init__(
        self,
        ctx: ContextManager | None = None,
        *,
        redis,
        pg_pool,
        model: str = "qwen-plus-us",
        prompts: dict[int, str] | None = None,
        game_modes: dict[str, Any] | None = None,
        tools: list | None = None,
        tool_handlers: dict | None = None,
        temperature: float = 0.6,
        max_tokens: int | None = None,
        max_iterations: int = 5,
        tool_timeout: float = 60.0,
    ):
        self.ctx = ctx
        self._redis = redis
        self._pg_pool = pg_pool
        self._prompts: dict[int, str] = prompts or {}
        self._game_modes: dict[str, Any] = game_modes or {}
        self._session_seeded: bool = False

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
        )
        self._model = model
        self.runner = AgentRunner(runner_config)

    def _create_default_tools(self):
        """Create the default tool set: web_search + volume_control + list_active_roasts + start_roast + mark_roast_complete."""
        from tools import (
            create_web_search_tool, volume_tool,
            create_list_roasts_tool, create_start_roast_tool,
        )
        from tools.roast import create_roast_complete_tool
        from tools.search import TavilyProvider
        from core.agent import ToolRegistry
        registry = ToolRegistry()
        registry.register(create_web_search_tool(TavilyProvider()))
        registry.register(volume_tool)
        registry.register(create_roast_complete_tool(
            redis=self._redis,
            pg_pool=self._pg_pool,
        ))
        if self._pg_pool:
            registry.register(create_list_roasts_tool(self._pg_pool))
            registry.register(create_start_roast_tool(
                self._pg_pool,
                redis=self._redis,
            ))
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
        interrupt_event: asyncio.Event | None = None,
        session_id: str | None = None,
    ) -> AsyncIterator[str | FlushSentinel]:
        """Complete reply pipeline: load context  ->  assemble  ->  stream  ->  persist.

        The single entry point for the voice bridge. Handles:
        - Context loading from Redis/PG
        - System prompt injection
        - Roast game routing (consume pending, inject body, tick)
        - Turn persistence after stream completes

        ``session_id`` is the LiveKit session ID, used as the KV cache
        routing key for sticky session affinity.
        """
        if not user_text.strip():
            return

        TelemetryCollector.mark("agent_req")

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

        # 4. Seed session info on first turn — after history, before user message
        if not self._session_seeded:
            session_msg = Message.system(self.build_session_info())
            messages.insert(-1, session_msg)  # before user message, after history
            asyncio.create_task(self._persist_turns(user_id, [session_msg]))
            self._session_seeded = True

        # 5. Check roast routing
        roast_state = await self.get_active_roast(user_id)
        game_mode = None
        if roast_state:
            if roast_state.phase == Phase.SETTLED or roast_state.phase == Phase.CLOSED:
                # Roast is over — close and enter Free Chat
                await self.close_roast(user_id)
                roast_state = None
            else:
                # ACTIVE or CLOSING — load game mode
                mode_id = str(roast_state.mode) if hasattr(roast_state, "mode") else ""
                game_mode = self._game_modes.get(mode_id)

        TelemetryCollector.mark("ctx_done")

        # 5. Persist user message before streaming — fire-and-forget so it doesn't block LLM
        if self.ctx and user_id:
            asyncio.create_task(self._persist_turns(user_id, [new_msg]))

        # 6. Stream and collect response
        response_chunks: list[str] = []
        logger.info(
            f"[PigAgent] generate_reply user={user_id} persona={persona_id} "
            f"roast={roast_state is not None} msgs={len(messages)}"
        )

        first_yield = True
        pre_stream_count = len(messages)  # messages added after this = runner output

        TelemetryCollector.mark("llm_req")

        # Make user context available to tool handlers via contextvars
        token_user = _current_user_id.set(user_id)
        token_persona = _current_persona_id.set(persona_id)
        try:
            if roast_state and game_mode:
                async for text in self._stream_roast(
                    messages, roast_state, game_mode,
                    interrupt_event=interrupt_event,
                    session_id=session_id,
                ):
                    if first_yield:
                        TelemetryCollector.mark("llm_internal")
                        first_yield = False
                    if isinstance(text, str):
                        response_chunks.append(text)
                    yield text
            else:
                async for text in self.runner.stream(messages, interrupt_event=interrupt_event, session_id=session_id):
                    if first_yield:
                        TelemetryCollector.mark("llm_internal")
                        first_yield = False
                    if isinstance(text, str):
                        response_chunks.append(text)
                    yield text
        finally:
            _current_user_id.reset(token_user)
            _current_persona_id.reset(token_persona)

        TelemetryCollector.mark("llm_end")
        logger.info(
            f"[PigAgent] Reply complete: {self.runner.last_step_count} steps, "
            f"status={self.runner.last_status}"
        )

        # 7. Persist runner-added messages (assistant, tool calls). User message was persisted before stream.
        if self.ctx and user_id:
            runner_msgs = self.runner.last_messages[pre_stream_count:] if self.runner.last_messages else []
            if runner_msgs:
                turn_no = await self._persist_turns(user_id, runner_msgs)
                if turn_no:
                    TelemetryCollector.set_meta("turn_number", turn_no)

    # ── Session ────────────────────────────────────────────────────────

    def build_session_info(self) -> str:
        """Build a one-time system message injected at conversation start."""
        now = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d %H:%M:%S %Z")
        return f"[Session Start]\nCurrent time: {now}"

    async def seed_session_info(self, user_id: str) -> None:
        """Persist session-info system message at the start of a new conversation."""
        if not self.ctx or not user_id:
            return
        msg = self.build_session_info()
        try:
            await self.ctx.add_turn(user_id=user_id, role="system", content=msg)
        except Exception as e:
            logger.warning(f"[PigAgent] Failed to seed session info: {e}")

    # ── Low-level stream (no side effects, used by tests) ──────────────

    async def stream(
        self,
        messages: list[Message],
        *,
        persona_id: int = 1,
        search: dict | None = None,
        interrupt_event: asyncio.Event | None = None,
    ) -> AsyncIterator[str | FlushSentinel]:
        """Low-level ReAct loop. No context loading, no persistence."""
        prompt = self._prompts.get(persona_id, "")
        if prompt:
            messages = [Message.system(prompt)] + messages

        async for text in self.runner.stream(
            messages, search=search, interrupt_event=interrupt_event,
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
    ) -> AsyncIterator[str | FlushSentinel]:
        """Start a roast game and stream the opening reply.

        Called by the API service after resolving the scenario from PG.
        Creates the roast session, persists the roast body to context,
        then triggers generate_reply() to deliver the opening lines.
        Yields text chunks for TTS playback.
        """
        from roast.activate import activate_roast

        try:
            instance_id, body = await activate_roast(
                user_id=user_id,
                persona_id=persona_id,
                roast_id=roast_id,
                game_mode=mode_id,
                prompt=prompt,
                redis=self._redis,
                pg_pool=self._pg_pool,
            )
        except Exception as e:
            logger.error(f"[PigAgent] activate_roast failed: {e}")
            return

        # Persist roast body to context  -  loaded automatically on each turn
        if body and self.ctx:
            try:
                await self.ctx.add_turn(
                    user_id=user_id,
                    role="system",
                    content=body,
                )
            except Exception as e:
                logger.error(f"[PigAgent] Failed to persist roast body: {e}")

        logger.info(
            f"[PigAgent] Roast started: {instance_id} "
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
        *,
        interrupt_event: asyncio.Event | None = None,
        session_id: str | None = None,
    ) -> AsyncIterator[str | FlushSentinel]:
        """Roast pipeline: consume pending  ->  stream  ->  tick.

        Roast body (news + game rules) was already persisted to context
        by start_roast()  -  it loads via ctx.load() in generate_reply().
        """
        token_user = _current_user_id.set(roast_state.user_id)
        token_persona = _current_persona_id.set(roast_state.persona_id)

        # 1. Consume pending trigger prompt
        try:
            pending_prompt = await consume(roast_state.roast_instance_id, self._redis)
            if pending_prompt:
                messages.append(Message.system(f"[Game Event]\n{pending_prompt}"))
        except Exception as e:
            logger.warning(f"[PigAgent] consume_pending failed: {e}")

        # 2. Stream LLM
        try:
            async for text in self.runner.stream(messages, interrupt_event=interrupt_event, session_id=session_id):
                yield text

            # 3. Tick  -  only in ACTIVE phase; skip during CLOSING
            if roast_state.phase == Phase.ACTIVE:
                asyncio.create_task(self._tick_roast(roast_state, game_mode, messages))
        finally:
            _current_user_id.reset(token_user)
            _current_persona_id.reset(token_persona)

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

    async def _persist_turns(
        self, user_id: str, messages: list[Message],
    ) -> int:
        """Persist all new messages to Redis/PG.

        Returns the first turn number, or 0 if nothing was persisted.
        """
        if not messages:
            return 0
        ctx = self._require_ctx()
        first_turn = 0
        try:
            for msg in messages:
                tool_calls_raw = None
                if msg.tool_calls:
                    tool_calls_raw = [
                        {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                        for tc in msg.tool_calls
                    ]
                turn_no = await ctx.add_turn(
                    user_id=user_id,
                    role=msg.role,
                    content=msg.content or "",
                    tool_calls=tool_calls_raw,
                    tool_call_id=msg.tool_call_id,
                    name=msg.name,
                    partial=msg.partial,
                )
                if not first_turn:
                    first_turn = turn_no
            return first_turn
        except Exception as e:
            logger.error(f"[PigAgent] persist_turns failed: {e}")
            return 0

    def _require_ctx(self) -> ContextManager:
        if self.ctx is None:
            raise RuntimeError("PigAgent created without ContextManager")
        return self.ctx
