# pigagent/core/agent/runner.py
"""AgentRunner  -  generic ReAct agent loop with composable stop conditions.

Stateless design: all per-call mutable state (current_step, AgentState,
last_result) is held in local variables. The instance itself is pure config
+ executor  -  safe to reuse across concurrent calls.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Callable

from loguru import logger

from collections.abc import AsyncIterator, Awaitable

from ..llm import get_llm
from ..llm.registry import ModelRegistry
from ..llm.types import Message, ChatResponse
from .stop import StepResult, step_count_is, no_tool_calls

from livekit.agents.types import FlushSentinel

from .state import AgentState, StateStatus
from .executor import ToolExecutor
from .interrupt import get_interrupt_manager, InterruptedException

BeforeStepHook = Callable[[], Awaitable[list]]
"""Called once before the loop. Returns initial messages for the LLM."""

AfterStepHook = Callable[[list, AgentState], Awaitable[None]]
"""Called once after the loop (in finally). Receives all messages and final state."""


@dataclass
class RunnerConfig:
    """Configuration for an AgentRunner instance."""

    model: str = "qwen-plus-us"
    tools: list = field(default_factory=list)
    tool_handlers: dict = field(default_factory=dict)
    tool_timeout: float = 60.0
    max_tool_concurrency: int = 10
    max_steps: int = 5
    stop_when: list[Callable] = field(default_factory=list)
    temperature: float = 0.6
    max_tokens: int | None = None
    interrupt_key: str | None = None


class AgentRunner:
    """ReAct agent loop runner  -  config + executor, zero per-call allocation.

    All mutable state (step counter, AgentState, last result) is local to
    each stream() / run() call. The instance is safe for concurrent reuse.
    """

    def __init__(self, config: RunnerConfig):
        self._model = config.model
        # Resolve to API-level model name (e.g., qwen-plus-us-cn → qwen-plus-us)
        info = ModelRegistry.get(config.model)
        self._api_model = info.api_model or config.model
        self._tools = config.tools
        self._tool_handlers = config.tool_handlers
        self._stop_when = config.stop_when or [
            step_count_is(config.max_steps),
            no_tool_calls,
        ]
        self._temperature = config.temperature
        self._max_tokens = config.max_tokens
        self._interrupt_key = config.interrupt_key

        self._executor = ToolExecutor(
            handlers=config.tool_handlers,
            default_timeout=config.tool_timeout,
            max_concurrency=config.max_tool_concurrency,
        )

        # Snapshot from the last completed call.
        self.last_step_count: int = 0
        self.last_status: str = ""
        self.last_messages: list[Message] = []

    # ── Public ──────────────────────────────────────────────────────────

    async def run(
        self,
        *,
        on_before_step: BeforeStepHook,
        on_after_step: AfterStepHook,
        session_id: str | None = None,
    ) -> StepResult:
        """Run the agent loop. Flushes results in finally."""
        if self._interrupt_key:
            return await self._run_guarded(on_before_step, on_after_step, session_id=session_id)
        return await self._run_loop(on_before_step, on_after_step, session_id=session_id)

    async def stream(
        self,
        messages: list[Message],
        *,
        search: dict | None = None,
        interrupt_event: asyncio.Event | None = None,
        session_id: str | None = None,
    ) -> AsyncIterator[str | FlushSentinel]:
        """Stream the ReAct loop, yielding text chunks and FlushSentinel for TTS.

        All state is local  -  safe for concurrent calls on the same instance.

        ``session_id`` is passed to the provider as the KV cache routing key
        (sticky session affinity)."""

        extra: dict[str, object] = {}
        if session_id:
            extra["session_id"] = session_id
        msgs = list(messages)
        state = AgentState(status=StateStatus.RUNNING.value)

        # Interrupt event passed directly from bridge — no manager lookup needed
        event = interrupt_event

        collected: list[str] = []

        try:
            while not self._should_stop(state):
                # Check interrupt before each LLM step
                if event and event.is_set():
                    raise InterruptedException()

                state.current_step += 1
                step = state.current_step
                logger.debug(f"[Runner] Stream step {step}")

                provider = get_llm(self._model)
                openai_tools = (
                    [t.to_openai_schema() for t in self._tools] if self._tools else None
                )

                collected = []
                tool_calls: list | None = None
                finish = "stop"
                _reply_yielded: set[int] = set()  # tool_call indices whose filler_text was already yielded
                _stripped_reply: str = ""  # filler_text to strip from second LLM response

                async for delta in provider.chat_stream(  # type: ignore[reportGeneralTypeIssues]
                    messages=msgs,
                    model=self._model,
                    tools=openai_tools,
                    temperature=self._temperature,
                    max_tokens=self._max_tokens,
                    search=search,
                    **extra,  # pyright: ignore[reportArgumentType]
                ):
                    # Check interrupt during streaming (between chunks)
                    if event and event.is_set():
                        raise InterruptedException()

                    if delta.content:
                        text = delta.content
                        if _stripped_reply and text.startswith(_stripped_reply):
                            text = text[len(_stripped_reply):]
                            _stripped_reply = ""  # only strip first chunk
                        collected.append(text)
                        yield text

                    if delta.tool_calls:
                        tool_calls = delta.tool_calls
                        for tc in delta.tool_calls:
                            text = _pull_filler_text(tc)
                            if text and tc.index not in _reply_yielded:
                                _reply_yielded.add(tc.index)
                                yield text  # → TTS only, not added to context
                                yield FlushSentinel()  # commit TTS immediately
                                _stripped_reply = text  # strip from next LLM step

                    if delta.finish_reason:
                        finish = delta.finish_reason

                content = "".join(collected)
                state.last_had_tool_calls = bool(tool_calls)

                if tool_calls:
                    msgs.append(self._make_assistant_msg(
                        StepResult(tool_calls=tool_calls, content=content, finish_reason=finish)
                    ))
                    exec_result = await self._executor.run(tool_calls)
                    for tr in exec_result.results:
                        msgs.append(Message.tool(
                            call_id=tr.tool_call_id,
                            name=tr.tool_name,
                            content=tr.content if tr.success else f"Error: {tr.error}",
                        ))
                        if tr.inject:
                            for inj in tr.inject:
                                msgs.append(Message(role=inj["role"], content=inj["content"]))
                    continue

                msgs.append(self._make_assistant_msg(
                    StepResult(content=content, finish_reason=finish)
                ))
                state.status = StateStatus.SUCCESS.value
                self.last_step_count = state.current_step
                self.last_status = state.status
                self.last_messages = msgs
                return

        except InterruptedException:
            state.status = StateStatus.INTERRUPTED.value
            partial = "".join(collected)
            if partial:
                msgs.append(Message.assistant(content=partial, partial=True))
            self.last_step_count = state.current_step
            self.last_status = state.status
            self.last_messages = msgs
            logger.info(f"[Runner] Interrupted at step {state.current_step}")
        except Exception:
            state.status = StateStatus.ERROR.value
            self.last_step_count = state.current_step
            self.last_status = state.status
            raise

    # ── Internal: interrupt-guarded loop ────────────────────────────────

    async def _run_guarded(
        self, on_before_step: BeforeStepHook, on_after_step: AfterStepHook,
        *,
        session_id: str | None = None,
    ) -> StepResult:
        """Race the main loop against an interrupt event."""
        key = self._interrupt_key
        assert key is not None, "_run_guarded requires interrupt_key"
        manager = get_interrupt_manager()
        event = manager.get(key)
        if event is None:
            event = manager.create(key)

        loop_task = asyncio.create_task(
            self._run_loop(on_before_step, on_after_step, session_id=session_id)
        )
        int_task = asyncio.create_task(event.wait())

        try:
            done, _ = await asyncio.wait(
                [loop_task, int_task], return_when=asyncio.FIRST_COMPLETED
            )

            if loop_task in done:
                int_task.cancel()
                try:
                    await int_task
                except asyncio.CancelledError:
                    pass
                exc = loop_task.exception()
                if exc is None:
                    return loop_task.result()
                raise exc
            else:
                loop_task.cancel()
                try:
                    await loop_task
                except asyncio.CancelledError:
                    pass
                self.last_status = StateStatus.INTERRUPTED.value
                logger.info(f"[Runner] Interrupted: {key}")
                return StepResult(finish_reason="interrupted")
        finally:
            manager.cleanup(key)

    # ── Internal: main loop ─────────────────────────────────────────────

    async def _run_loop(
        self, on_before_step: BeforeStepHook, on_after_step: AfterStepHook,
        *,
        session_id: str | None = None,
    ) -> StepResult:
        """Load context once, loop in memory, flush in finally."""
        messages: list = []
        state = AgentState(status=StateStatus.RUNNING.value)
        last_result: StepResult | None = None

        try:
            loaded = await on_before_step()
            messages = list(loaded) if loaded else []
            while not self._should_stop(state):
                state.current_step += 1
                logger.debug(f"[Runner] Step {state.current_step}")

                result = await self._run_step(messages, session_id=session_id)
                last_result = result
                state.last_had_tool_calls = bool(result.tool_calls)

                if result.tool_calls:
                    messages.append(self._make_assistant_msg(result))
                    exec_result = await self._executor.run(result.tool_calls)
                    for tr in exec_result.results:
                        messages.append(Message.tool(
                            call_id=tr.tool_call_id,
                            name=tr.tool_name,
                            content=tr.content if tr.success else f"Error: {tr.error}",
                        ))
                        if tr.inject:
                            for inj in tr.inject:
                                messages.append(Message(role=inj["role"], content=inj["content"]))
                    continue

                messages.append(self._make_assistant_msg(result))
                break

        except InterruptedException:
            state.status = StateStatus.INTERRUPTED.value
            logger.info(f"[Runner] Interrupted at step {state.current_step}")
        except Exception as e:
            logger.error(f"[Runner] Step {state.current_step} failed: {e}")
            state.status = StateStatus.ERROR.value
        finally:
            if state.is_running:
                state.status = StateStatus.SUCCESS.value
            await on_after_step(messages, state)
            self.last_step_count = state.current_step
            self.last_status = state.status
            logger.info(
                f"[Runner] Loop ended: {state.current_step} steps, "
                f"status={state.status}, flushed {len(messages)} messages"
            )

        return last_result or StepResult()

    # ── Internal: single step (non-streaming) ───────────────────────────

    async def _run_step(self, messages: list[Message], *, session_id: str | None = None) -> StepResult:
        """Execute a single LLM call and parse the response."""
        provider = get_llm(self._model)
        openai_tools = (
            [t.to_openai_schema() for t in self._tools] if self._tools else None
        )

        extra: dict[str, object] = {}
        if session_id:
            extra["session_id"] = session_id

        response: ChatResponse = await provider.chat(
            messages=messages,
            model=self._model,
            tools=openai_tools,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            **extra,  # pyright: ignore[reportArgumentType]
        )

        tool_calls = response.tool_calls if response.tool_calls else None
        finish_reason = response.finish_reason or (
            "tool_calls" if tool_calls else "stop"
        )

        return StepResult(
            messages=messages,
            content=response.content or "",
            tool_calls=tool_calls,
            finish_reason=finish_reason,
        )

    # ── Internal: helpers ───────────────────────────────────────────────

    def _should_stop(self, state: AgentState) -> bool:
        for condition in self._stop_when:
            try:
                if condition(state):
                    logger.info(
                        f"[Runner] Stop condition met: "
                        f"{getattr(condition, '__name__', condition)}"
                    )
                    return True
            except Exception as e:
                logger.warning(f"[Runner] Stop condition error: {e}")
        return False

    @staticmethod
    def _make_assistant_msg(result: StepResult) -> Message:
        """Build an assistant Message from a StepResult."""
        return Message.assistant(
            content=result.content,
            tool_calls=result.tool_calls,
        )


# ── filler_text helpers ─────────────────────────────────────────────────────

import re as _re

_FILLER_TEXT_RE = _re.compile(r'"(?:filler_text|user_reply)"\s*:\s*"((?:[^"\\]|\\.)*)"')


def _pull_filler_text(tc) -> str | None:
    """Extract filler_text from a streaming tool_call's arguments.

    filler_text is always the first JSON field. Once the closing quote is
    found, return the content so the runner can yield it for TTS.
    """
    args = tc.arguments or ""
    if not args:
        return None
    m = _FILLER_TEXT_RE.search(args)
    if m is None:
        return None
    # Only yield once the field is complete (comma or closing brace follows)
    end = m.end()
    if end < len(args) and args[end] not in (",", "}"):
        return None
    text = m.group(1)
    return _re.sub(r'\\(.)', r'\1', text)
