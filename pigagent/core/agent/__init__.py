# pigagent/core/agent/__init__.py
"""Core agent infrastructure — runner, state, interrupt, executor, stop conditions."""

from .stop import StepResult, step_count_is, no_tool_calls
from .state import AgentState, StateStatus
from .interrupt import InterruptManager, InterruptedException, get_interrupt_manager, check_interrupt

from .executor import ToolExecutor, ToolResult, ToolExecutionResult
from .runner import AgentRunner, RunnerConfig
from .sanitize import _len_fallback, validate_tool_calls

__all__ = [
    "StepResult",
    "step_count_is", "no_tool_calls",
    "AgentState", "StateStatus",
    "InterruptManager", "InterruptedException", "get_interrupt_manager", "check_interrupt",

    "ToolExecutor", "ToolResult", "ToolExecutionResult",
    "AgentRunner", "RunnerConfig",
    "_len_fallback", "validate_tool_calls",
]
