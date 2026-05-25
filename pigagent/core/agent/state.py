# pigagent/core/agent/state.py
"""Agent state — per-chat data object, created fresh for each stream() / run().

Holds all mutable state for a single agent loop execution. Stop conditions
and hooks read from it. AgentRunner carries no per-call state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class StateStatus(Enum):
    """Agent request lifecycle states."""

    RUNNING = "running"
    SUCCESS = "success"
    FAIL = "fail"
    ERROR = "error"
    INTERRUPTED = "interrupted"


@dataclass
class AgentState:
    """Per-chat data object — one per stream() / run() call.

    Created fresh at the start of each agent loop. Holds step counter,
    tool call tracking, and terminal status. Read by stop conditions and
    after-step hooks.
    """

    status: str = field(default=StateStatus.RUNNING.value)
    current_step: int = 0
    last_had_tool_calls: bool = False

    _exception: Exception | None = field(default=None, repr=False)
    _traceback: str | None = field(default=None, repr=False)
    _fail_reason: str | None = field(default=None, repr=False)

    @property
    def is_terminal(self) -> bool:
        return self.status in {
            StateStatus.SUCCESS.value,
            StateStatus.FAIL.value,
            StateStatus.ERROR.value,
            StateStatus.INTERRUPTED.value,
        }

    @property
    def is_running(self) -> bool:
        return self.status == StateStatus.RUNNING.value
