# pigagent/metrics/session.py
"""Per-session cold-start latency collector — async-safe singleton.

Tracks the time from AgentSession.run() entry to "Agent ready",
broken into declarative segments. Same pattern as TurnMetrics.

Usage (in session.py):

    from metrics.session import ColdStartMetrics

    ColdStartMetrics.start(session_id=ctx.job.id, room_name=ctx.room.name)
    ColdStartMetrics.mark("stt_tts")
    ColdStartMetrics.set_meta("stt_provider", "deepgram")
    ColdStartMetrics.flush()
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
# Cold start latency = entry → ready
#
#   Segment         | Formula                      | What it measures
#   ----------------+------------------------------+--------------------------------
#   dispatch_lag / room_age | wall_entry - room_created | LiveKit Cloud: room creation → agent receives job.
#                            |                          | If <60s: new room → dispatch_lag.
#                            |                          | If >=60s: reused room → room_age.
#   stt_init        | stt_init - entry             | Metadata + get_stt() — STT singleton init (first session per worker)
#   tts_init        | tts_init - stt_init          | create_tts() — TTS per-session creation
#   vad             | vad - tts_init               | get_vad() — Silero VAD model load (first session per worker)
#   session_setup   | session_start - vad          | AgentSession() construction + event handlers
#   lk_connect      | session_started - session_start | session.start() — LiveKit room connect + RoomIO
#   wait_participant| user_id - session_started    | Resolve user_id (from metadata or wait for join)
#   agent_create    | agent_created - user_id      | create_pig_agent() — ContextManager + PigAgent
#   finalize        | ready - agent_created        | Track source fixup + final config logging

SEGMENTS: list[tuple[str, str, str]] = [
    ("dispatch_lag",      "dispatch_lag",    "dispatch_lag"),
    ("stt_init",          "entry",           "stt_init"),
    ("tts_init",          "stt_init",        "tts_init"),
    ("vad",               "tts_init",        "vad"),
    ("session_setup",     "vad",             "session_start"),
    ("lk_connect",        "session_start",   "session_started"),
    ("wait_participant",  "session_started", "user_id"),
    ("agent_create",      "user_id",         "agent_created"),
    ("finalize",          "agent_created",   "ready"),
]

META_KEYS = [
    "stt_provider", "llm_model", "tts_model",
    "persona_id", "user_id", "room_name", "session_id",
]

# ── Per-session storage ────────────────────────────────────────────

_session: dict[str, Any] | None = None
_session_counter: int = 0


def _make_session(session_id: str, room_name: str) -> dict[str, Any]:
    global _session_counter
    _session_counter += 1
    return {
        "session_n": _session_counter,
        "session_id": session_id,
        "room_name": room_name,
        "marks": {},
        "meta": {},
    }


# ── Public API ─────────────────────────────────────────────────────

class ColdStartMetrics:
    """Async-safe singleton for session cold-start latency.

    Call class methods from anywhere. One session at a time — calling
    start() flushes any previous session.
    """

    @classmethod
    def start(cls, *, session_id: str, room_name: str,
             room_creation_time: float = 0.0) -> None:
        global _session
        if _session is not None:
            cls.flush()
        _session = _make_session(session_id, room_name)
        _session["wall_entry"] = time.time()
        _session["room_creation_time"] = room_creation_time
        cls.mark("entry")

    @classmethod
    def mark(cls, key: str) -> None:
        if _session is not None:
            _session["marks"][key] = time.perf_counter()

    @classmethod
    def set_meta(cls, key: str, value: object) -> None:
        if _session is not None:
            _session["meta"][key] = value

    @classmethod
    def flush(cls) -> None:
        global _session
        if _session is None:
            return
        sess = _session
        _session = None

        # Require at least the "ready" mark to consider complete
        if "ready" not in sess["marks"]:
            return

        _log(sess)


# ── Internal ───────────────────────────────────────────────────────

def _log(sess: dict[str, Any]) -> None:
    m = sess["marks"]
    total = _diff(m, "entry", "ready")

    # Compute LiveKit-side delta from room creation to agent receiving job.
    # New room (<60s): room was created by this dispatch → dispatch_lag.
    # Reused room (>=60s): room already existed → show room_age for reference.
    wall_entry = sess.get("wall_entry")
    room_created = sess.get("meta", {}).get("room_creation_time", 0.0)
    lk_delta: float | None = None
    lk_delta_label: str = ""
    if wall_entry and room_created:
        try:
            if isinstance(room_created, (int, float)) and 0 < room_created <= wall_entry:
                delta = round(wall_entry - room_created, 3)
                if delta < 60.0:
                    lk_delta = delta
                    lk_delta_label = "dispatch_lag"
                    m["dispatch_lag"] = delta
                else:
                    lk_delta = delta
                    lk_delta_label = "room_age"
        except TypeError:
            pass

    # ── Agent-side segments ──
    seg_parts: list[str] = []
    for label, a, b in SEGMENTS:
        if label == "dispatch_lag":
            continue  # handled above
        d = _diff(m, a, b)
        if d is not None:
            seg_parts.append(f"{label}={_fmt(d)}")

    # ── Assemble output ──
    if lk_delta is not None:
        seg_parts.insert(0, f"{lk_delta_label}={_fmt(lk_delta)}")

    meta_parts: list[str] = []
    meta = sess["meta"]
    for k in META_KEYS:
        v = meta.get(k)
        if v is not None and v != "":
            meta_parts.append(f"{k}={v}")

    logger.bind(session_id=sess["session_id"]).info(
        f"[COLDSTART n={sess['session_n']}] TOTAL={_fmt(total)}  "
        f"{'  '.join(seg_parts)}"
        + (f"  [{', '.join(meta_parts)}]" if meta_parts else "")
    )

    if _PG_DSN:
        try:
            asyncio.ensure_future(_pg_write(sess, m))
        except RuntimeError:
            pass


async def _pg_write(sess: dict[str, Any], m: dict[str, float]) -> None:
    import json as _json
    import asyncpg  # type: ignore[import-untyped]

    segments: dict[str, float | None] = {}
    for label, a, b in SEGMENTS:
        segments[label] = _diff(m, a, b)
    segments["total"] = _diff(m, "entry", "ready")

    try:
        conn = await asyncpg.connect(_PG_DSN)
        try:
            await conn.execute(
                """INSERT INTO coldstart_metrics
                   (session_id, room_name, marks, segments, meta)
                   VALUES ($1, $2, $3::jsonb, $4::jsonb, $5::jsonb)""",
                sess.get("session_id", ""),
                sess.get("room_name", ""),
                _json.dumps(m),
                _json.dumps({k: v for k, v in segments.items() if v is not None}),
                _json.dumps({k: v for k, v in sess["meta"].items()
                            if v is not None and v != ""}),
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


def _fmt(v: float | None) -> str:
    return f"{v:.3f}s" if v is not None else "—"
