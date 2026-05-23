# pigagent/core/agent/state.py
"""Agent state machine — status tracking for React agent loops (voice-first).

State transitions:
  RUNNING     ──→ SUCCESS       (agent loop completed normally)
  RUNNING     ──→ ERROR         (unexpected exception)
  RUNNING     ──→ INTERRUPTED   (user VAD / explicit cancel)
  (any)       ──→ FAIL          (stop condition met, e.g. max iterations)

One AgentState per request (= one user turn = one agent loop execution).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class StateStatus(Enum):
    """Agent request lifecycle states.

    Covers all paths in a voice agent loop:
      - Normal completion (SUCCESS)
      - Exception (ERROR)
      - User interrupt via VAD (INTERRUPTED)
      - Safety stop, e.g. max iterations (FAIL)
    """

    RUNNING = "running"
    SUCCESS = "success"
    FAIL = "fail"
    ERROR = "error"
    INTERRUPTED = "interrupted"


@dataclass
class AgentState:
    """Per-request state carried through a single agent loop execution.

    'request' = one user turn = one AgentRunner.run() invocation.
    For pigugu's one-user-one-session model, a new AgentState is created
    for each turn within the entrypoint coroutine.
    """

    status: str = field(default=StateStatus.RUNNING.value)

    # ── Runtime-only (not serialized) ───────────────────────────────────

    _exception: Exception | None = field(default=None, repr=False)
    _traceback: str | None = field(default=None, repr=False)
    _fail_reason: str | None = field(default=None, repr=False)

    # ── Properties ──────────────────────────────────────────────────────

    @property
    def is_terminal(self) -> bool:
        """True if the state is terminal (loop should exit)."""
        return self.status in {
            StateStatus.SUCCESS.value,
            StateStatus.FAIL.value,
            StateStatus.ERROR.value,
            StateStatus.INTERRUPTED.value,
        }

    @property
    def is_running(self) -> bool:
        return self.status == StateStatus.RUNNING.value
