# pigagent/metrics/turn.py
"""Per-turn latency collector — thin emit facade over metrics/registry + scope.

Business code calls the class methods below (synchronous, in-memory, never
raises, never touches a sink). Scope ownership, two-phase finish/flush and the
ClickHouse export all live behind ``metrics.registry`` / ``metrics.exporter``.

Usage (any module, zero setup):

    TurnMetrics.start_turn(user_id="web-xxx", persona_id=1)
    TurnMetrics.mark("vad_start")
    TurnMetrics.set_meta("llm_model", "qwen-plus")
    TurnMetrics.finish_turn()   # child task: freeze
    # owner task, after awaiting the turn's async work:
    TurnMetrics._flush_turn()   # enqueue to exporter
"""

from __future__ import annotations

from typing import Any

from metrics import registry
from metrics.registry import _current as _current_var  # re-exported for voice


class TurnMetrics:
    """Classmethod facade: operate on the current task's active turn scope."""

    @classmethod
    def start_turn(cls, *, user_id: str, persona_id: int) -> None:
        """Open a fresh turn scope, closing+flushing any prior active one."""
        registry.open(user_id=user_id, persona_id=persona_id)

    @classmethod
    def has_mark(cls, key: str) -> bool:
        scope = _active()
        return bool(scope is not None and scope.has_mark(key))

    @classmethod
    def mark(cls, key: str) -> None:
        scope = _active()
        if scope is not None:
            scope.mark(key)

    @classmethod
    def mark_time(cls, key: str) -> float | None:
        scope = _active()
        return scope.mark_time(key) if scope is not None else None

    @classmethod
    def set_mark(cls, key: str, value: float) -> None:
        """Store an arbitrary perf_counter value (e.g. rebuilt from age)."""
        scope = _active()
        if scope is not None:
            scope.set_mark(key, value)

    @classmethod
    def set_meta(cls, key: str, value: object) -> None:
        scope = _active()
        if scope is not None:
            scope.set_meta(key, value)

    @classmethod
    def finish_turn(cls) -> None:
        """Freeze the current turn (child-task call). Does NOT enqueue."""
        registry.finish_current()

    @classmethod
    def _flush_turn(cls) -> None:
        """Owner call: freeze + enqueue the active turn, clear the contextvar."""
        registry.flush_current()


def _active():
    """Active scope, but only if it is not finished (matches the old
    ``_resolve_active_turn`` contract so child tasks cannot write into a turn
    the owner already flushed)."""
    scope = registry.current()
    if scope is None or scope.finished:
        return None
    return scope


# Backward-compat alias (agent.py / voice/pipecat import this name).
TelemetryCollector = TurnMetrics
