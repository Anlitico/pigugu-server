# pigagent/core/agent/executor.py
"""ToolExecutor — concurrent tool execution with timeout, error isolation, and status tracking.

Executes tool calls from LLM responses. Supports both serial and concurrent modes.
Each tool result is returned as a structured dict for appending to agent history.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from loguru import logger

from ..llm.types import ToolCall


@dataclass
class ToolResult:
    """Result of a single tool execution."""

    tool_call_id: str
    tool_name: str
    success: bool
    content: str = ""
    error: str | None = None
    duration_ms: float = 0.0


@dataclass
class ToolExecutionResult:
    """Aggregate result of executing one or more tool calls."""

    results: list[ToolResult] = field(default_factory=list)
    total_duration_ms: float = 0.0

    @property
    def all_success(self) -> bool:
        return all(r.success for r in self.results)


ToolHandler = Callable[[dict], Any]
"""A tool handler receives the parsed arguments dict and returns a result (str or dict)."""


class ToolExecutor:
    """Executes tool calls with configurable timeout and concurrency.

    Usage:
        executor = ToolExecutor(handlers={"get_weather": weather_handler})
        result = await executor.run(tool_calls, timeout=30.0)
    """

    def __init__(
        self,
        handlers: Dict[str, ToolHandler],
        *,
        default_timeout: float = 60.0,
        max_concurrency: int = 10,
    ):
        self._handlers = handlers
        self._default_timeout = default_timeout
        self._semaphore = asyncio.Semaphore(max_concurrency)

    # ── Public ──────────────────────────────────────────────────────────

    async def run(
        self,
        tool_calls: list[ToolCall],
        *,
        timeout: float | None = None,
        concurrent: bool = True,
    ) -> ToolExecutionResult:
        """Execute one or more tool calls.

        Args:
            tool_calls: Tool calls from the LLM response.
            timeout: Per-tool timeout in seconds (default: self._default_timeout).
            concurrent: If True, execute all tools concurrently via asyncio.gather.

        Returns:
            ToolExecutionResult with individual results and aggregate timing.
        """
        t0 = time.monotonic()
        t_out = timeout if timeout is not None else self._default_timeout

        if not tool_calls:
            return ToolExecutionResult()

        if concurrent and len(tool_calls) > 1:
            tasks = [
                asyncio.create_task(self._execute_one(tc, t_out))
                for tc in tool_calls
            ]
            try:
                results = await asyncio.gather(*tasks, return_exceptions=True)
            except BaseException:
                # Cancel all pending tasks on interrupt / cancellation
                for t in tasks:
                    if not t.done():
                        t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise
            tool_results = [
                r if isinstance(r, ToolResult) else ToolResult(
                    tool_call_id=tc.id,
                    tool_name=tc.name,
                    success=False,
                    error=str(r) if r else "cancelled",
                )
                for tc, r in zip(tool_calls, results)
            ]
        else:
            tool_results = []
            for tc in tool_calls:
                result = await self._execute_one(tc, t_out)
                tool_results.append(result)

        return ToolExecutionResult(
            results=tool_results,
            total_duration_ms=(time.monotonic() - t0) * 1000,
        )

    async def run_single(self, tool_call: ToolCall, *, timeout: float | None = None) -> ToolResult:
        """Execute a single tool call."""
        return await self._execute_one(tool_call, timeout if timeout is not None else self._default_timeout)

    # ── Internal ────────────────────────────────────────────────────────

    async def _execute_one(self, tc: ToolCall, timeout: float) -> ToolResult:
        t0 = time.monotonic()
        handler = self._handlers.get(tc.name)

        if handler is None:
            logger.warning(f"[Executor] No handler for tool: {tc.name}")
            return ToolResult(
                tool_call_id=tc.id,
                tool_name=tc.name,
                success=False,
                error=f"No handler registered for tool '{tc.name}'",
                duration_ms=(time.monotonic() - t0) * 1000,
            )

        # Parse arguments
        try:
            args = json.loads(tc.arguments) if tc.arguments else {}
        except json.JSONDecodeError:
            return ToolResult(
                tool_call_id=tc.id,
                tool_name=tc.name,
                success=False,
                error=f"Invalid JSON arguments: {tc.arguments}",
                duration_ms=(time.monotonic() - t0) * 1000,
            )

        # Execute with timeout and concurrency limit
        async with self._semaphore:
            try:
                result = await asyncio.wait_for(
                    self._call_handler(handler, args),
                    timeout=timeout,
                )
                content = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
                duration_ms = (time.monotonic() - t0) * 1000
                logger.debug(f"[Executor] {tc.name} completed in {duration_ms:.0f}ms")
                return ToolResult(
                    tool_call_id=tc.id,
                    tool_name=tc.name,
                    success=True,
                    content=content,
                    duration_ms=duration_ms,
                )
            except asyncio.TimeoutError:
                duration_ms = (time.monotonic() - t0) * 1000
                logger.warning(f"[Executor] {tc.name} timed out after {timeout:.0f}s")
                return ToolResult(
                    tool_call_id=tc.id,
                    tool_name=tc.name,
                    success=False,
                    error=f"Tool execution timed out after {timeout:.0f}s",
                    duration_ms=duration_ms,
                )
            except Exception as e:
                duration_ms = (time.monotonic() - t0) * 1000
                logger.error(f"[Executor] {tc.name} failed: {e}")
                return ToolResult(
                    tool_call_id=tc.id,
                    tool_name=tc.name,
                    success=False,
                    error=str(e),
                    duration_ms=duration_ms,
                )

    @staticmethod
    async def _call_handler(handler: ToolHandler, args: dict) -> Any:
        """Call the handler (sync or async) with parsed arguments."""
        result = handler(args)
        if inspect_is_coroutine(result):
            result = await result
        return result


def inspect_is_coroutine(obj: Any) -> bool:
    """Check if an object is a coroutine (without importing inspect)."""
    return hasattr(obj, "__await__")
