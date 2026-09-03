"""Pure helpers: segment math, E2E computation and CH row building.

Everything here is deterministic over a scope's ``marks``/``meta`` — no I/O,
no asyncio. Shared by the exporter (CH ``metrics.*`` rows) and by the voice
pipeline's telemetry snapshot, so the two can never disagree on the math.

Segment model
-------------
E2E (server-perspective) = stt_final → agent_spk (xiaozhi firmware: turn-end
is purely server-side). Main-chain segments are strictly serial and sum to
E2E; diagnostic segments may overlap / go negative / be absent and must
never be summed into E2E.
"""

from __future__ import annotations

import time
from typing import Any

from metrics.scope import now_unix_ms

# ── Segment model (kept in sync with the pgsql-era metrics/turn.py) ─────

# (label, start_mark, end_mark). Main chain first, diagnostics after.
SEGMENTS: list[tuple[str, str, str]] = [
    ("agent_init",  "stt_final",   "agent_init"),
    ("orchestrator", "agent_init", "agent_req"),
    ("context",     "agent_req",   "ctx_done"),
    ("llm_prep",    "ctx_done",    "llm_req"),
    ("llm_ttft",    "llm_req",     "llm_first_token"),
    ("llm_to_tts",  "llm_first_token", "tts_first_ready"),
    ("tts_ttfb",    "tts_first_ready", "agent_spk"),
    ("stt",         "server_received_vad_at", "stt_final"),
    ("vad",         "vad_start",   "vad_end"),
    ("server_vad",  "vad_end",     "stt_commit"),
    ("vad_to_recv", "vad_end",     "server_received_vad_at"),
    ("turn_end",    "stt_final",   "server_stop"),
    ("llm_rest",    "llm_first_token", "llm_end"),
    ("tts",         "tts_start",   "tts_end"),
    ("ctx_l1",      "agent_req",   "ctx_l1_done"),
    ("ctx_l2",      "ctx_l1_done", "ctx_l2_done"),
    ("ctx_roast",   "ctx_l2_done", "ctx_roast_done"),
]

MAIN_SEGMENT_LABELS: set[str] = {
    "agent_init", "orchestrator", "context",
    "llm_prep", "llm_ttft", "llm_to_tts", "tts_ttfb",
}

META_KEYS = [
    "stt_model", "llm_model", "tts_model",
    "prompt_tokens", "completion_tokens", "cached_tokens",
    "turn_phase", "device_playback_ms", "turn_number",
]


def ms_diff(m: dict[str, float], a: str, b: str) -> float | None:
    va, vb = m.get(a), m.get(b)
    if va is not None and vb is not None:
        return round((vb - va) * 1000.0, 1)
    return None


def compute_e2e(marks: dict[str, float]) -> float | None:
    """Server E2E = stt_final → agent_spk, with old-firmware fallbacks."""
    e2e = ms_diff(marks, "stt_final", "agent_spk")
    if e2e is None:
        e2e = ms_diff(marks, "server_received_vad_at", "agent_spk")
    if e2e is None:
        e2e = ms_diff(marks, "vad_end", "agent_spk")
    return e2e


def compute_stt_tail(marks: dict[str, float]) -> int:
    """STT endpointing tail = user_stop → stt_final (0 when no device anchor).

    ``vad_end`` is the server-clock reconstruction of the device's AFE
    "user finished speaking" report (see the firmware vad_silence age). This
    is the portion of perceived latency that the old stt_final-anchored E2E
    always hid.
    """
    return int(ms_diff(marks, "vad_end", "stt_final") or 0)


def compute_e2e_perceived(marks: dict[str, float]) -> int:
    """Perceived E2E = user_stop → first bot audio (agent_spk).

    Anchored on the device-reported ``vad_end`` when present; otherwise
    falls back to the server E2E (stt_final → agent_spk) so every reply turn
    still produces a value.
    """
    perceived = ms_diff(marks, "vad_end", "agent_spk")
    return int(perceived) if perceived is not None else int(compute_e2e(marks) or 0)


def build_segments(marks: dict[str, float]) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for label, a, b in SEGMENTS:
        if label == "orchestrator":
            # First main segment: stretch from stt_final when no agent_init
            # (warm follow-up turn) so the main chain still starts at E2E's t0.
            start = "agent_init" if marks.get("agent_init") is not None else "stt_final"
            out[label] = ms_diff(marks, start, "agent_req")
        else:
            out[label] = ms_diff(marks, a, b)
    return out


def split_roles(all_segments: dict[str, float | None]) -> tuple[dict[str, float], dict[str, float]]:
    main_ = {k: v for k, v in all_segments.items() if v is not None and k in MAIN_SEGMENT_LABELS}
    diag = {k: v for k, v in all_segments.items() if v is not None and k not in MAIN_SEGMENT_LABELS}
    return main_, diag


def _fmt(v: float | None) -> str:
    return f"{v:.1f}ms" if v is not None else "—"


def _iso_utc(ms: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def log_line(
    *,
    user_id: str,
    turn_id: int,
    marks: dict[str, float],
    event_unix_ms: dict[str, int],
    meta: dict[str, Any],
    e2e: float | None,
    main_segments: dict[str, float],
    diag_segments: dict[str, float],
) -> str:
    """Human-readable per-turn [METRIC] line (kept for prod logs)."""
    seg_parts: list[str] = []
    for label in ("agent_init", "orchestrator", "context", "llm_prep",
                  "llm_ttft", "llm_to_tts", "tts_ttfb"):
        seg_parts.append(f"{label}={_fmt(main_segments.get(label))}")
    for label in ("stt", "vad", "server_vad", "turn_end", "llm_rest", "tts",
                  "ctx_l1", "ctx_l2", "ctx_roast"):
        d = diag_segments.get(label)
        if d is not None:
            seg_parts.append(f"{label}={_fmt(d)}")

    meta_parts = []
    for k in META_KEYS:
        v = meta.get(k)
        if v is not None and v != "":
            meta_parts.append(f"{k}={v}")

    real_turn = meta.get("turn_number", "")
    tid = f"n={turn_id}"
    if real_turn:
        tid += f"(#={real_turn})"

    anchor_ms = (event_unix_ms.get("server_received_vad_at")
                 or event_unix_ms.get("stt_final") or event_unix_ms.get("vad_end"))
    time_str = ""
    if anchor_ms is not None:
        start = _iso_utc(anchor_ms)
        if event_unix_ms.get("agent_spk") is not None:
            start += f" → {_iso_utc(event_unix_ms['agent_spk'])}"
        time_str = f"  [{start}]"

    line = (f"[METRIC u={user_id} {tid}{time_str}] E2E={_fmt(e2e)}  "
            f"{'  '.join(seg_parts)}")
    if meta_parts:
        line += f"  [{', '.join(meta_parts)}]"
    return line


def _clean_meta(meta: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in meta.items() if v is not None and v != "" and v != 0}


def marks_json(marks: dict[str, float], event_unix_ms: dict[str, int]) -> dict[str, dict]:
    """{key: {perf_counter: float, unix_ms: int}} — forensic / recompute store."""
    out: dict[str, dict] = {}
    for k, perf in marks.items():
        entry: dict[str, Any] = {"perf_counter": perf}
        if k in event_unix_ms:
            entry["unix_ms"] = event_unix_ms[k]
        out[k] = entry
    return out


def segments_json(
    all_segments: dict[str, float | None],
    e2e: float | None,
) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for label, v in all_segments.items():
        if v is None:
            continue
        role = "main" if label in MAIN_SEGMENT_LABELS else "diagnostic"
        out[label] = {"role": role, "ms": v}
    if e2e is not None:
        out["e2e"] = {"role": "main", "ms": e2e}
    return out


def event_anchor_ms(event_unix_ms: dict[str, int]) -> int:
    """Best event timestamp for partitioning: agent_spk, else stt_final, else now."""
    return int(
        event_unix_ms.get("agent_spk")
        or event_unix_ms.get("stt_final")
        or now_unix_ms()
    )


# ── CH row builders (columns must match k8s/clickhouse-migration-job 0004) ──

TURN_TABLE = "metrics.turn_latency"
TURN_COLUMNS = ("ts_ms", "user_id", "turn_id", "persona_id",
                "turn_phase", "marks", "segments", "meta",
                "stt_tail_ms", "e2e_perceived_ms")


def turn_row(scope) -> tuple[str, tuple[str, ...], tuple[Any, ...]]:
    """TurnScope -> (table, columns, values) for one INSERT."""
    marks = scope.marks
    e2e = compute_e2e(marks)
    all_segments = build_segments(marks)
    payload = (
        event_anchor_ms(scope.event_unix_ms),
        scope.user_id,
        scope.turn_id,
        scope.persona_id,
        str(scope.meta.get("turn_phase", "") or ""),
        _json_dumps(marks_json(marks, scope.event_unix_ms)),
        _json_dumps(segments_json(all_segments, e2e)),
        _json_dumps(_clean_meta(scope.meta)),
        compute_stt_tail(marks),
        compute_e2e_perceived(marks),
    )
    return TURN_TABLE, TURN_COLUMNS, payload


SESSION_TABLE = "metrics.session"
SESSION_COLUMNS = ("ts_ms", "user_id", "device_id", "session_id",
                   "connect_hello_ms", "connect_first_audio_ms")


def session_row(scope) -> tuple[str, tuple[str, ...], tuple[Any, ...]]:
    """SessionScope -> one metrics.session row. connect_* are server-clock
    spans (perf_counter) from accept -> hello and accept -> first uplink frame."""
    a = scope.meta.get("accept_pc")
    h = scope.meta.get("hello_pc")
    f = scope.meta.get("first_audio_pc")
    hello_ms = int(round((h - a) * 1000)) if (a and h) else 0
    first_ms = int(round((f - a) * 1000)) if (a and f) else 0
    payload = (
        now_unix_ms(),
        scope.user_id,
        str(scope.meta.get("device_id", "") or ""),
        str(scope.meta.get("session_id", "") or ""),
        max(hello_ms, 0),
        max(first_ms, 0),
    )
    return SESSION_TABLE, SESSION_COLUMNS, payload


COMPRESSION_TABLE = "metrics.compression"
COMPRESSION_COLUMNS = ("ts_ms", "user_id", "scenario", "segments", "meta")

# (label, start_mark, end_mark) — seconds, mirroring the pgsql-era compressor.
COMPRESSION_PHASES: list[tuple[str, str, str]] = [
    ("check", "start", "check_done"),
    ("llm", "check_done", "llm_done"),
    ("profile", "llm_done", "profile_done"),
    ("total", "start", "end"),
]


def compression_segments(marks: dict[str, float]) -> dict[str, float]:
    out: dict[str, float] = {}
    for label, a, b in COMPRESSION_PHASES:
        va, vb = marks.get(a), marks.get(b)
        if va is not None and vb is not None:
            out[label] = round(vb - va, 2)  # seconds (legacy parity)
    return out


def compression_row(scope) -> tuple[str, tuple[str, ...], tuple[Any, ...]]:
    scenario = str(scope.meta.get("scenario", "free_chat") or "free_chat")
    payload = (
        now_unix_ms(),
        scope.user_id,
        scenario,
        _json_dumps(compression_segments(scope.marks)),
        _json_dumps(_clean_meta({k: v for k, v in scope.meta.items()
                                 if k != "scenario"})),
    )
    return COMPRESSION_TABLE, COMPRESSION_COLUMNS, payload


def _json_dumps(obj: object) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False)
