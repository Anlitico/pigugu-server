"""PigAgent — pigugu agent: owns all conversation logic.

The agent lifecycle:
    1. on_user_turn_completed — filler, search flag, lifecycle hooks
    2. generate_reply — context assembly, roast prompts, LLM + tools, tick
    3. stream — raw LLM ReAct loop (used by generate_reply)

LiveKit is fully abstracted — main.py only wires connections.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Callable

from loguru import logger

from core.llm.types import Message, ToolSpec
from core.agent.runner import AgentRunner, RunnerConfig
from core.agent.stop import StepResult, no_tool_calls
from context.manager import ContextManager
from tools.search.utils import build_search_messages

ToolHandler = Callable[[Any], Any]


@dataclass
class PigAgentConfig:
    """Configuration for a PigAgent instance."""

    model: str = "qwen3.6-plus"
    system_prompt_id: str = ""
    tools: list[ToolSpec] = field(default_factory=list)
    tool_handlers: dict[str, ToolHandler] = field(default_factory=dict)
    temperature: float = 0.6
    max_tokens: int | None = None
    max_iterations: int = 5
    tool_timeout: float = 60.0
    interrupt_key: str | None = None


class PigAgent:
    """Pigugu agent — owns all conversation logic.

    LiveKit delegates to on_user_turn_completed() and generate_reply().
    stream() is the raw LLM entry point for direct message input.
    """

    def __init__(
        self,
        ctx: ContextManager | None,
        config: PigAgentConfig,
        *,
        game_mode=None,
        conv_manager=None,
        roast_body: str = "",
        fillers: list[str] | None = None,
        enable_filler_words: bool = False,
        enable_policy_search: bool = False,
    ):
        self.ctx = ctx
        self.config = config
        self._game_mode = game_mode
        self._conv_manager = conv_manager
        self._roast_body = roast_body
        self._fillers = fillers or []
        self._enable_filler_words = enable_filler_words
        self._enable_policy_search = enable_policy_search

        # Per-turn state
        self._pending_filler: str | None = None
        self._filler_yielded_at: float | None = None
        self._use_search: bool = False
        self._roast_injected: bool = False

        runner_config = RunnerConfig(
            model=config.model,
            tools=config.tools,
            tool_handlers=config.tool_handlers,
            tool_timeout=config.tool_timeout,
            max_steps=config.max_iterations,
            stop_when=[no_tool_calls],
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            interrupt_key=config.interrupt_key,
        )
        self.runner = AgentRunner(runner_config)

    def configure(
        self,
        *,
        game_mode=None,
        conv_manager=None,
        roast_body: str = "",
        fillers: list[str] | None = None,
        enable_filler_words: bool = False,
        enable_policy_search: bool = False,
    ) -> None:
        """Post-creation configuration for runtime dependencies."""
        self._game_mode = game_mode
        self._conv_manager = conv_manager
        self._roast_body = roast_body if roast_body else self._roast_body
        self._fillers = fillers or []
        self._enable_filler_words = enable_filler_words
        self._enable_policy_search = enable_policy_search

    # ── LiveKit hooks ────────────────────────────────────────────────────

    async def on_user_turn_completed(self, turn_ctx, new_message):
        """Called when the user's turn ends, before LLM generates reply."""
        user_text = (new_message.text_content or "").strip()

        # Filler words (skip short messages)
        if self._enable_filler_words and self._fillers and len(user_text.split()) > 5:
            filler = random.choice(self._fillers)
            self._pending_filler = filler
            turn_ctx.add_message(
                role="system",
                content=f'You already began your reply with: "{filler}". '
                        f"Continue from there. Do NOT repeat it.",
            )
            logger.info(f"💬 [FILLER] Queued filler: \"{filler}\"")

        # Policy search flag
        self._use_search = self._enable_policy_search
        if self._use_search:
            logger.info("🔍 [SEARCH] Native web search enabled")

        # Lifecycle hooks
        if self._conv_manager:
            lifecycle_result = await self._conv_manager.on_user_turn_completed(user_text)
            if lifecycle_result:
                if lifecycle_result.get("ending_triggered"):
                    review_tone = lifecycle_result.get("review_tone", "")
                    if review_tone:
                        turn_ctx.add_message(role="system", content=review_tone)
                    ending_line = lifecycle_result.get("ending_line", "")
                    if ending_line:
                        logger.info(f"🏁 [LIFECYCLE] Ending line: {ending_line[:80]}...")
                if lifecycle_result.get("mode_context"):
                    turn_ctx.add_message(
                        role="system", content=lifecycle_result["mode_context"]
                    )

    async def generate_reply(self, chat_ctx) -> AsyncIterator[str]:
        """Generate a reply: assemble context, inject prompts, stream LLM + tools.

        Called from llm_node. Yields text chunks for TTS.
        """
        if self._conv_manager:
            await self._conv_manager.assemble_context(chat_ctx)

        filler = self._pending_filler
        self._pending_filler = None
        use_search = self._use_search
        self._use_search = False

        # Convert LiveKit items → Message list
        dict_msgs = build_search_messages(chat_ctx.items)
        messages = [Message(role=m["role"], content=m["content"]) for m in dict_msgs]

        # Inject roast body once
        if self._roast_body and not self._roast_injected:
            self._roast_injected = True
            roast_msg = Message.user(self._roast_body)
            insert_at = 0
            for i, msg in enumerate(messages):
                if msg.role != "system":
                    insert_at = i
                    break
            else:
                insert_at = len(messages)
            messages.insert(insert_at, roast_msg)
            logger.info(f"[Roast] Injected roast body at position {insert_at}")

        search_param = {"enabled": True} if use_search else None

        def _gen():
            return self.stream(messages, search=search_param)

        if filler:
            self._filler_yielded_at = time.perf_counter()
            yield filler + " "
            chat_ctx.add_message(role="assistant", content=filler)

            queue = asyncio.Queue()

            async def _buffer_llm():
                try:
                    async for chunk in _gen():
                        await queue.put(chunk)
                except Exception as e:
                    logger.error(f"[LLM] Stream failed: {e}")
                finally:
                    await queue.put(None)

            asyncio.create_task(_buffer_llm())
            await asyncio.sleep(3.0)
            asyncio.create_task(_buffer_llm())
            await asyncio.sleep(3.0)

            while True:
                chunk = await queue.get()
                if chunk is None:
                    break
                yield chunk
        else:
            async for chunk in _gen():
                yield chunk

    # ── Raw streaming ────────────────────────────────────────────────────

    async def stream(
        self,
        messages: list[Message],
        *,
        search: dict | None = None,
        interrupt_key: str | None = None,
    ) -> AsyncIterator[str]:
        """Stream the agent ReAct loop on given messages."""
        effective_key = interrupt_key or self.config.interrupt_key
        logger.info(f"[PigAgent] Starting stream, interrupt_key={effective_key}")

        async for text in self.runner.stream(
            messages, search=search, interrupt_key=effective_key,
        ):
            yield text

        logger.info(
            f"[PigAgent] Stream complete: {self.runner.current_step} steps, "
            f"status={self.runner.state.status}"
        )

    # ── Context-managed run ──────────────────────────────────────────────

    async def run(self, *, user_id: str, interrupt_key: str | None = None) -> StepResult:
        """Run the agent loop with context from Redis/PG (non-streaming)."""
        from prompts import get as get_system_prompt

        effective_key = interrupt_key or self.config.interrupt_key
        self.runner._interrupt_key = effective_key
        ctx = self._require_ctx()
        initial_count = 0

        async def _load_context() -> list[Message]:
            nonlocal initial_count
            context = await ctx.load(user_id=user_id)
            prompt = get_system_prompt(self.config.system_prompt_id)
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
            f"[PigAgent] Run complete: {self.runner.current_step} steps, "
            f"status={self.runner.state.status}"
        )
        return result

    # ── Context helpers ──────────────────────────────────────────────────

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

    async def end_roast(self, user_id: str) -> None:
        await self._require_ctx().end_roast(user_id)

    async def write_game_state(self, *, user_id: str, state: dict) -> None:
        await self._require_ctx().write_game_state(user_id=user_id, state=state)
