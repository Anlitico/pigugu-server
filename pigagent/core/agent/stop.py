# pigagent/core/agent/stop.py
"""Loop stop conditions and StepResult for AgentRunner.

StepResult  -  what a single LLM + tool-execution step produced.
Stop conditions  -  composable predicates on AgentState.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .state import AgentState


@dataclass
class StepResult:
    """Result of a single agent step (LLM call + optional tool execution)."""

    messages: list = field(default_factory=list)
    content: str = ""
    tool_calls: list | None = None
    finish_reason: str = ""  # "stop" | "tool_calls" | "length" | "interrupted"


# ── Stop conditions (composable  -  stop when any returns True) ──────────


def step_count_is(max_steps: int) -> Callable[[AgentState], bool]:
    """Stop after max_steps iterations."""

    def _check(state: AgentState) -> bool:
        return state.current_step >= max_steps

    _check.__name__ = f"step_count_is({max_steps})"
    return _check


def no_tool_calls(state: AgentState) -> bool:
    """Stop when the last step had no tool calls."""
    return state.current_step > 0 and not state.last_had_tool_calls
