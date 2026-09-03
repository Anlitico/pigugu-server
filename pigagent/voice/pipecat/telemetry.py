"""Telemetry snapshot for TurnStorage — reads the current turn Scope.

Pipecat runs each FrameProcessor in its own asyncio task with an isolated
``contextvars`` copy, so a scope bound in one processor is invisible in the
others. The observer therefore shares the live ``PiguguTurnScope`` on
``PiguguTurnState.active_turn`` and processors re-bind it into the contextvar
(:func:`ensure_turn_context`) wherever they touch telemetry — the same
capture/restore pattern the old connection.py used for its Deepgram callbacks.
All interval math lives in ``metrics.render`` so the CH row and this snapshot
can never disagree.
"""

from __future__ import annotations

from typing import Any

from metrics import render
from metrics.registry import _current as _turn_var
from metrics.scope import Scope
from voice.pipecat.state import PiguguTurnState


def _marks_meta(obj: Any) -> tuple[dict[str, float], dict[str, Any]]:
    if isinstance(obj, Scope):
        return obj.marks, obj.meta
    if isinstance(obj, dict):
        return obj.get("marks", {}) or {}, obj.get("meta", {}) or {}
    return {}, {}


def ensure_turn_context(state: PiguguTurnState) -> None:
    """Bind this task's contextvar to the turn's shared scope.

    Call before reading/writing telemetry in any processor task, so
    ``TelemetryCollector.mark/set_meta/has_mark`` operate on the scope the
    observer opened. Idempotent and O(1).
    """
    if state.active_turn is not None:
        _turn_var.set(state.active_turn)


def telemetry_snapshot(
    *,
    device_playback_ms: int = 0,
    turn: Any = None,
) -> dict[str, Any]:
    """Port of connection.py's ``_commit_turn_storage`` snapshot.

    ``device_playback_ms`` is passed separately because it lives in the shared
    turn state (validated from the device tts_played ack), not in the scope's
    meta. ``turn`` is the captured scope/dict for THIS turn (from
    ``state.active_turn`` at turn start); falls back to the current task's
    contextvar when omitted.
    """
    obj = turn if turn is not None else _turn_var.get()
    marks, meta = _marks_meta(obj)
    e2e_ms = render.compute_e2e(marks) or 0
    stt_ms = render.ms_diff(marks, "server_received_vad_at", "stt_final") or 0
    llm_ttft_ms = render.ms_diff(marks, "llm_req", "llm_first_token") or 0
    tts_ttfb_ms = render.ms_diff(marks, "tts_first_ready", "agent_spk") or 0
    return {
        "e2e_ms": e2e_ms,
        "stt_ms": stt_ms,
        "llm_ttft_ms": llm_ttft_ms,
        "tts_ttfb_ms": tts_ttfb_ms,
        "device_playback_ms": device_playback_ms or int(meta.get("device_playback_ms", 0) or 0),
        "llm_model": meta.get("llm_model", ""),
    }
