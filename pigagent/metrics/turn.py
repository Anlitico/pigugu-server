# pigagent/metrics/turn.py
"""Per-turn latency collector — async-safe singleton for conversation turns.

Usage (any module, zero setup):

    from metrics.turn import TurnMetrics

    TurnMetrics.start_turn(user_id="web-xxx", persona_id=1)
    TurnMetrics.mark("vad_start")
    TurnMetrics.set_meta("llm_model", "qwen-plus")
    TurnMetrics.finish_turn()
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from loguru import logger

_PG_DSN: str = os.getenv("DATABASE_URL", "").replace("+asyncpg", "")

# ── Segment breakdown ──────────────────────────────────────────────
#
# User perceived latency:
#   stop_speaking (~0ms) + vad silence (~500ms) + E2E
#
#   E2E = vad_end → agent_spk
#
#   Segment      | Formula                  | What it measures
#   -------------+--------------------------+--------------------------------
#   vad          | vad_end - vad_start      | User speech + VAD silence (not in E2E)
#   stt          | stt_final - vad_end      | Deepgram transcription
#   lk_pipeline  | agent_req - stt_final    | LiveKit turn switch → bridge → generate_reply
#   ctx_load     | ctx_done - agent_req     | Context load + system prompt + roast
#   llm_prep     | llm_req - ctx_done       | Roast body + misc before LLM call
#   llm_api      | llm_internal - llm_req   | LLM API request → first token (TTFT)
#   llm_out      | llm_ttft - llm_internal  | First token → bridge yield
#   synth_gap    | agent_spk - llm_ttft     | Bridge yield → TTS starts playing audio
#   llm_rest     | llm_end - llm_ttft       | Remaining LLM (parallel TTS, not in E2E)
#   tts          | tts_end - tts_start      | TTS duration (not in E2E)
#
# E2E ≈ stt + lk_pipeline + ctx_load + llm_prep + llm_api + llm_out + synth_gap
#
# Metadata: stt_model, llm_model, tts_model, prompt_tokens,
#           completion_tokens, cached_tokens

SEGMENTS: list[tuple[str, str, str]] = [
    ("vad",         "vad_start",   "vad_end"),
    ("stt",         "vad_end",     "stt_final"),
    ("lk_pipeline", "stt_final",   "agent_req"),
    ("ctx_load",    "agent_req",   "ctx_done"),
    # ── ctx_load sub-segments (diagnostic) ──
    ("ctx_l1",      "agent_req",   "ctx_l1_done"),
    ("ctx_l2",      "ctx_l1_done", "ctx_l2_done"),
    ("ctx_roast",   "ctx_l2_done", "ctx_roast_done"),
    # ── LLM pipeline ──
    ("llm_prep",    "ctx_done",    "llm_req"),
    ("llm_api",     "llm_req",     "llm_internal"),
    ("llm_out",     "llm_internal", "llm_ttft"),
    ("synth_gap",   "llm_ttft",    "agent_spk"),
    ("llm_rest",    "llm_ttft",    "llm_end"),
    ("tts",         "tts_start",   "tts_end"),
]

META_KEYS = [
    "stt_model", "llm_model", "tts_model",
    "prompt_tokens", "completion_tokens", "cached_tokens",
]

# ── Per-turn storage ─────────────────────────────────────────────────

_current: dict[str, Any] | None = None
_turn_counter: int = 0


def _make_turn(user_id: str, persona_id: int) -> dict[str, Any]:
    global _turn_counter
    _turn_counter += 1
    return {
        "turn_id": _turn_counter,
        "user_id": user_id,
        "persona_id": persona_id,
        "marks": {},
        "meta": {},
    }


# ── Public API ───────────────────────────────────────────────────────

class TurnMetrics:
    """Async-safe singleton. Call class methods from anywhere — each
    asyncio task gets its own turn dict via contextvars."""

    @classmethod
    def start_turn(cls, *, user_id: str, persona_id: int) -> None:
        global _current
        if _current is not None:
            cls.finish_turn()
        _current = _make_turn(user_id, persona_id)

    @classmethod
    def has_mark(cls, key: str) -> bool:
        if _current is None:
            return False
        return key in _current["marks"]

    @classmethod
    def mark(cls, key: str) -> None:
        if _current is not None:
            _current["marks"][key] = time.perf_counter()

    @classmethod
    def set_meta(cls, key: str, value: object) -> None:
        if _current is not None:
            _current["meta"][key] = value
            if key == "turn_number" and isinstance(value, int):
                _current["turn_id"] = value

    @classmethod
    def finish_turn(cls) -> None:
        global _current
        if _current is None:
            return
        turn = _current
        _current = None
        _log(turn)


# ── Internal ─────────────────────────────────────────────────────────

def _log(turn: dict[str, Any]) -> None:
    m = turn["marks"]
    if m.get("llm_start") is None:
        return
    e2e = _diff(m, "vad_end", "agent_spk")

    seg_parts: list[str] = []
    for label, a, b in SEGMENTS:
        d = _diff_nonneg(m, a, b) if label == "stt" else _diff(m, a, b)
        if d is not None:
            seg_parts.append(f"{label}={_fmt(d)}")

    meta_parts: list[str] = []
    meta = turn["meta"]
    for k in META_KEYS:
        v = meta.get(k)
        if v is not None and v != "":
            meta_parts.append(f"{k}={v}")

    real_turn = meta.get("turn_number", "")
    tid = f"n={turn['turn_id']}"
    if real_turn:
        tid += f"(#={real_turn})"
    logger.bind(user=turn["user_id"], turn=turn["turn_id"]).info(
        f"[METRIC u={turn['user_id']} {tid}] E2E={_fmt(e2e)}  "
        f"{'  '.join(seg_parts)}"
        + (f"  [{', '.join(meta_parts)}]" if meta_parts else "")
    )

    if _PG_DSN:
        try:
            asyncio.ensure_future(_pg_write(turn, m, e2e))
        except RuntimeError:
            pass


async def _pg_write(turn: dict[str, Any], m: dict[str, float], e2e: float | None) -> None:
    import json as _json
    import asyncpg  # type: ignore[import-untyped]

    segments: dict[str, float | None] = {}
    for label, a, b in SEGMENTS:
        segments[label] = _diff(m, a, b)
    segments["e2e"] = _diff(m, "vad_end", "agent_spk")

    try:
        conn = await asyncpg.connect(_PG_DSN)
        try:
            await conn.execute(
                """INSERT INTO metrics
                   (user_id, turn_id, persona_id, marks, segments, meta)
                   VALUES ($1,$2,$3, $4::jsonb, $5::jsonb, $6::jsonb)
                   ON CONFLICT (user_id, turn_id) DO NOTHING""",
                turn["user_id"],
                turn["turn_id"],
                turn.get("persona_id", 0),
                _json.dumps(m),
                _json.dumps({k: v for k, v in segments.items() if v is not None}),
                _json.dumps({k: v for k, v in turn["meta"].items()
                            if v is not None and v != "" and v != 0}),
            )
        finally:
            await conn.close()
    except Exception:
        pass


def _diff(m: dict[str, float], a: str, b: str) -> float | None:
    va, vb = m.get(a), m.get(b)
    if va is not None and vb is not None:
        return round(vb - va, 3)
    return None


def _diff_nonneg(m: dict[str, float], a: str, b: str) -> float | None:
    d = _diff(m, a, b)
    if d is not None and d < 0:
        return 0.0
    return d


def _fmt(v: float | None) -> str:
    return f"{v:.3f}s" if v is not None else "—"


# Backward compat alias
TelemetryCollector = TurnMetrics
