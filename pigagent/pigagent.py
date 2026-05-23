# pigagent/pigagent.py
"""PigAgent — pigugu agent definition that assembles AgentRunner + ContextManager.

The agent lifecycle:
    1. Load context (L1-L4) from ContextManager
    2. Run AgentRunner loop (LLM + tools)
    3. Record turns to ContextManager
    4. Trigger compression when needed

Streaming is supported via the AgentRunner's on_after_step hook.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from loguru import logger

from core.llm.types import Message, ToolSpec
from core.agent.runner import AgentRunner, RunnerConfig
from core.agent.stop import StepResult, no_tool_calls

from context.manager import ContextManager
from prompts import get as get_system_prompt

ToolHandler = Callable[[Any], Any]


@dataclass
class PigAgentConfig:
    """Configuration for a PigAgent instance."""

    model: str = "qwen3.6-plus"
    system_prompt_id: str = ""          # ID in the prompt registry
    tools: list[ToolSpec] = field(default_factory=list)
    tool_handlers: dict[str, ToolHandler] = field(default_factory=dict)
    temperature: float = 0.6
    max_tokens: int | None = None
    max_iterations: int = 5
    tool_timeout: float = 60.0
    interrupt_key: str | None = None


class PigAgent:
    """Pigugu agent — owns ContextManager and delegates loop to AgentRunner.

    Usage:
        ctx = ContextManager(redis_client=redis, pg_pool=pg)
        config = PigAgentConfig(
            model="qwen3.6-plus",
            system_prompt_id="trump",
            tools=[...],
            tool_handlers={...},
        )
        agent = PigAgent(ctx, config)
        messages = agent.load(user_id="u1")         # assemble context
        result = await agent.run(user_id="u1")       # run agent loop
        agent.record_turn(user_id, result)           # record to context
    """

    def __init__(self, ctx: ContextManager, config: PigAgentConfig):
        self.ctx = ctx
        self.config = config

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

    # ── Convenience wrappers around ContextManager ──────────────────────

    async def load(self, *, user_id: str) -> list[Message]:
        """Assemble context for an LLM call. Delegates to ContextManager.load."""
        return await self.ctx.load(user_id=user_id)

    async def add_turn(
        self, user_id: str, role: str, content: str, *,
        tool_calls: list | None = None,
        tool_call_id: str | None = None,
        name: str | None = None,
        partial: bool = False,
    ) -> None:
        """Record a turn to context. Delegates to ContextManager.add_turn."""
        await self.ctx.add_turn(
            user_id=user_id, role=role, content=content,
            tool_calls=tool_calls, tool_call_id=tool_call_id,
            name=name, partial=partial,
        )

    async def end_roast(self, user_id: str) -> None:
        await self.ctx.end_roast(user_id)

    async def write_game_state(self, *, user_id: str, state: dict) -> None:
        await self.ctx.write_game_state(user_id=user_id, state=state)

    # ── Main entry point ────────────────────────────────────────────────

    async def run(self, *, user_id: str, interrupt_key: str | None = None) -> StepResult:
        """Run the agent loop with pigugu context assembly.

        Loads context once, loops in memory, flushes all messages to Redis/PG
        in finally — guaranteed on interrupt, error, or normal completion.
        """
        effective_key = interrupt_key or self.config.interrupt_key
        self.runner._interrupt_key = effective_key

        # Track how many messages were already in context (don't re-save them)
        initial_count = 0

        async def _load_context() -> list[Message]:
            nonlocal initial_count
            context = await self.ctx.load(user_id=user_id)
            # Prepend system prompt from registry
            prompt = get_system_prompt(self.config.system_prompt_id)
            if prompt:
                context.insert(0, Message.system(prompt))
            initial_count = len(context)
            return context

        async def _flush_all(messages: list, state) -> None:
            """Save new messages (beyond initial context) to Redis/PG."""
            for msg in messages[initial_count:]:
                tool_calls = None
                if msg.tool_calls:
                    tool_calls = [
                        {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                        for tc in msg.tool_calls
                    ]
                await self.ctx.add_turn(
                    user_id=user_id,
                    role=msg.role,
                    content=msg.content,
                    tool_calls=tool_calls,
                    tool_call_id=msg.tool_call_id,
                    name=msg.name,
                )

        logger.info(f"[PigAgent] Starting run for user={user_id}, interrupt_key={effective_key}")
        result = await self.runner.run(
            on_before_step=_load_context,
            on_after_step=_flush_all,
        )
        logger.info(
            f"[PigAgent] Run complete: {self.runner.current_step} steps, "
            f"status={self.runner.state.status}, new_messages={len(result.messages) - initial_count}"
        )
        return result
