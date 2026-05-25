"""PigAgent — pigugu voice agent: LLM orchestration, context, tools, roast.

Owns all content-level logic (system prompt, context assembly, tool execution,
game mode triggers). The voice bridge (lk.bridge) handles the LiveKit
pipeline integration — PigAgent itself has zero LiveKit dependency.

Public API:
    PigAgent(model=...)        — create an agent
    agent.stream(messages)     — raw LLM ReAct loop (used by bridge)
    agent.run(user_id=...)     — context-managed standalone run
    agent.load / add_turn / ... — context helpers
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from loguru import logger

from core.llm.types import Message
from core.agent.runner import AgentRunner, RunnerConfig
from core.agent.stop import StepResult, no_tool_calls
from context.manager import ContextManager


class PigAgent:
    """Pigugu agent — LLM orchestration and content logic.

    Zero LiveKit dependency. The bridge (livekit.bridge.PigAgentVoiceBridge)
    handles all voice pipeline integration.
    """

    def __init__(
        self,
        ctx: ContextManager | None = None,
        *,
        model: str = "qwen3.6-plus",
        prompts: dict[str, str] | None = None,
        tools: list | None = None,
        tool_handlers: dict | None = None,
        temperature: float = 0.6,
        max_tokens: int | None = None,
        max_iterations: int = 5,
        tool_timeout: float = 60.0,
        interrupt_key: str | None = None,
    ):
        self.ctx = ctx
        self._prompts: dict[str, str] = prompts or {}

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

    # ── Raw streaming (primary interface for bridge) ────────────────────

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
            f"[PigAgent] Starting stream, persona={persona_id}, "
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

    # ── Context-managed run (standalone, non-LiveKit) ────────────────────

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
