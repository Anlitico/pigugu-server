# agent/core/agent/stop.py
"""Loop stop conditions and step result types for AgentRunner.

StepResult: what a single LLM call + tool execution produced.
step_count_is / no_tool_calls: composable stop conditions for the agent loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class StepResult:
    """Result of a single agent step (LLM call + optional tool execution)."""

    messages: list = field(default_factory=list)
    content: str = ""                   # LLM response text for this step
    tool_calls: list | None = None
    finish_reason: str = ""             # "stop" | "tool_calls" | "length" | "interrupted"


# ── Stop conditions (composable — stop when any returns True) ──────────────


def step_count_is(max_steps: int) -> Callable:
    """Stop after max_steps iterations."""

    def _check(runner: Any) -> bool:
        return runner.current_step >= max_steps

    _check.__name__ = f"step_count_is({max_steps})"
    return _check


def no_tool_calls(runner: Any) -> bool:
    """Stop when the last result had no tool calls."""
    return runner.last_result is not None and not runner.last_result.tool_calls
