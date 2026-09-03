"""Current-scope ownership for turn telemetry.

Pipecat runs each FrameProcessor in its own asyncio task, so a ``contextvars``
value set by one processor is invisible to another. The registry keeps ONE
active turn scope per task via a contextvar, plus explicit ``bind`` for
cross-task handoff: the observer opens the scope (its task now owns it), then
the TTS bridge ``bind``s the same scope into its own task so every mark made
while it runs the agent/LLM/TTS lands in the right record.

Lifecycle mirrors the old two-phase contract:
- ``open`` closes+enqueues any prior active scope (lazy flush of the last turn).
- ``finish_current`` (child task) only freezes the scope; the owner flushes.
- ``flush_current`` (owner) freezes + hands the scope to the exporter.
"""

from __future__ import annotations

import contextvars

from metrics.exporter import enqueue
from metrics.scope import Scope, TurnScope

_current: contextvars.ContextVar[Scope | None] = contextvars.ContextVar(
    "pigagent_telemetry_scope", default=None
)


def current() -> Scope | None:
    """The active scope in this task, or None."""
    return _current.get()


def open(user_id: str, persona_id: int) -> TurnScope:
    """Close + enqueue any prior active scope, then open a fresh turn scope."""
    prior = _current.get()
    if prior is not None:
        _current.set(None)
        _flush(prior)
    scope = TurnScope(user_id=user_id, persona_id=persona_id)
    _current.set(scope)
    return scope


def _flush(scope: Scope) -> None:
    """Freeze + hand one scope to the exporter, dropping phantom turns.

    A turn whose VAD window produced neither a server sentence nor bot audio
    (wake-word burst / noise blip) has no latency observation to contribute —
    it is finished and discarded instead of writing an empty CH row.
    """
    scope.finish()
    if isinstance(scope, TurnScope) and not scope.meaningful:
        return
    enqueue(scope)


def bind(scope: Scope) -> None:
    """Point this task's contextvar at an existing scope (cross-task handoff)."""
    _current.set(scope)


def finish_current() -> None:
    """Freeze the active scope without enqueuing (child-task safety)."""
    scope = _current.get()
    if scope is not None and not scope.finished:
        scope.finish()


def flush_current() -> None:
    """Freeze + enqueue the active scope and clear the contextvar (owner only)."""
    scope = _current.get()
    _current.set(None)
    if scope is not None:
        _flush(scope)
