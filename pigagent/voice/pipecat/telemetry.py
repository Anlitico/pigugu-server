"""Telemetry snapshot for TurnStorage — reads the current turn's marks/meta.

Pipecat runs each FrameProcessor in its own asyncio task with an isolated
``contextvars`` copy, so the ``TelemetryCollector`` contextvar set by one
processor (e.g. the turn-start in the observer) is invisible to the others.
We therefore share the live turn dict on ``PiguguTurnState.active_turn`` and
re-bind it into the contextvar wherever telemetry is touched — the same
capture/restore pattern the old connection.py used for its Deepgram callbacks.
"""

from __future__ import annotations

from typing import Any

from metrics.turn import _current_var as _turn_var  # type: ignore[attr-defined]
from voice.pipecat.state import PiguguTurnState


def _ms_diff(a: float | None, b: float | None) -> int | None:
    """perf_counter span in ms. None if either side is missing, 0 if b < a."""
    if a is None or b is None:
        return None
    if b < a:
        return 0
    return round((b - a) * 1000.0)


def ensure_turn_context(state: PiguguTurnState) -> None:
    """Bind the shared turn dict into this task's contextvar.

    Call at the top of any processor method that reads/writes telemetry, so
    ``TelemetryCollector.mark/set_meta/has_mark`` operate on the same dict the
    observer created. Idempotent and O(1).
    """
    if state.active_turn is not None:
        _turn_var.set(state.active_turn)


def telemetry_snapshot(
    *,
    device_playback_ms: int = 0,
    turn: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Port of connection.py's ``_commit_turn_storage`` snapshot.

    ``device_playback_ms`` is passed separately because it lives in the
    shared turn state (validated from the device tts_played ack), not in
    the telemetry meta. ``turn`` is the captured turn dict for THIS turn
    (from ``state.active_turn`` at turn start); falls back to the current
    task's contextvar when omitted.
    """
    t = turn if turn is not None else _turn_var.get() or {}
    marks = t.get("marks", {}) or {}
    e2e_ms = _ms_diff(marks.get("server_received_vad_at"), marks.get("agent_spk"))
    if e2e_ms is None:
        e2e_ms = _ms_diff(marks.get("vad_end"), marks.get("agent_spk")) or 0
    stt_ms = _ms_diff(marks.get("server_received_vad_at"), marks.get("stt_final")) or 0
    llm_ttft_ms = _ms_diff(marks.get("llm_req"), marks.get("llm_first_token")) or 0
    tts_ttfb_ms = _ms_diff(marks.get("tts_first_ready"), marks.get("agent_spk")) or 0
    meta = t.get("meta") or {}
    return {
        "e2e_ms": e2e_ms,
        "stt_ms": stt_ms,
        "llm_ttft_ms": llm_ttft_ms,
        "tts_ttfb_ms": tts_ttfb_ms,
        "device_playback_ms": device_playback_ms or int(meta.get("device_playback_ms", 0) or 0),
        "llm_model": meta.get("llm_model", ""),
    }
