"""Observability scope: pure in-memory record for one turn / event.

A Scope is a plain accumulator — it holds marks (perf_counter seconds +
parallel unix-ms wall stamps) and free-form meta. It has **no sinks and no
asyncio**: business code writes to it synchronously (O(1), never raises),
and a finished scope is handed to the exporter (metrics/export.py) by the
task that owns it.

Two subclasses carry the export shape for their domain:
``TurnScope``   -> ``metrics.turn_latency``  (per-turn latency breakdown)
``CompressionScope`` -> ``metrics.compression``
"""

from __future__ import annotations

import time
from typing import Any

_counter = 0


def now_unix_ms() -> int:
    """UTC milliseconds. ``time.time()`` is already UTC — no tz games."""
    return int(time.time() * 1000)


class Scope:
    """Accumulate marks/attrs for one logical event. Stops writing once finished.

    A finished scope must only be flushed (enqueued to the exporter) once and
    by the task that owns it; ``finish()`` sets the flag so a racing child task
    cannot keep writing into a scope its parent has already handed off.
    """

    __slots__ = (
        "turn_id", "user_id", "persona_id",
        "marks", "event_unix_ms", "meta",
        "_finished", "enqueued",
    )

    def __init__(self, *, user_id: str, persona_id: int, turn_id: int | None = None):
        global _counter
        _counter += 1
        self.turn_id: int = turn_id if turn_id is not None else _counter
        self.user_id: str = user_id
        self.persona_id: int = persona_id
        # perf_counter seconds (monotonic) — used to compute interval spans.
        self.marks: dict[str, float] = {}
        # Parallel UTC-ms stamps per mark — for cross-system alignment / logs.
        self.event_unix_ms: dict[str, int] = {}
        self.meta: dict[str, Any] = {}
        self._finished = False
        self.enqueued = False

    # ── emit (sync, in-memory, never raises, ignores writes after finish) ──

    def mark(self, key: str) -> None:
        if self._finished:
            return
        self.marks[key] = time.perf_counter()
        self.event_unix_ms[key] = now_unix_ms()

    def set_mark(self, key: str, value: float) -> None:
        """Store an arbitrary perf_counter value (e.g. a timestamp rebuilt
        from the firmware's ``user_stop_age_ms``). The unix stamp is recorded
        at *this* instant (a local approximation, not the remote event time)."""
        if self._finished:
            return
        self.marks[key] = value
        self.event_unix_ms[key] = now_unix_ms()

    def set_meta(self, key: str, value: object) -> None:
        if self._finished:
            return
        self.meta[key] = value
        if key == "turn_number" and isinstance(value, int):
            self.turn_id = value

    def has_mark(self, key: str) -> bool:
        return key in self.marks

    def mark_time(self, key: str) -> float | None:
        """Stored perf_counter for a mark, if any."""
        return self.marks.get(key)

    @property
    def finished(self) -> bool:
        return self._finished

    def finish(self) -> None:
        """Freeze this scope. Idempotent. Does NOT enqueue — the owning task
        flushes it (calls the exporter) after the turn's async work resolves,
        mirroring the old two-phase finish/flush contract."""
        self._finished = True


class TurnScope(Scope):
    """Per-conversation-turn scope; exports to ``metrics.turn_latency``."""

    @property
    def meaningful(self) -> bool:
        """A turn worth exporting: it produced a sentence (``stt_final`` —
        includes ``no_tts`` replies) or bot audio (``agent_spk``). Pure VAD
        blips / wake bursts reach neither and are dropped by the registry so
        they cannot flood the latency table with empty rows."""
        return "agent_spk" in self.marks or "stt_final" in self.marks

    def ch_row(self):
        from metrics import render
        return render.turn_row(self)

    def log_line(self) -> str:
        from metrics import render
        e2e = render.compute_e2e(self.marks)
        main_, diag = render.split_roles(render.build_segments(self.marks))
        return render.log_line(
            user_id=self.user_id,
            turn_id=self.turn_id,
            marks=self.marks,
            event_unix_ms=self.event_unix_ms,
            meta=self.meta,
            e2e=e2e,
            main_segments=main_,
            diag_segments=diag,
        )


class CompressionScope(Scope):
    """One compression run; exports to ``metrics.compression``. Constructed
    standalone (not via the registry) — compression runs as its own background
    task and must not touch the turn-scope contextvar."""

    def __init__(self, *, user_id: str, scenario: str = "free_chat"):
        super().__init__(user_id=user_id, persona_id=0)
        self.meta["scenario"] = scenario

    def ch_row(self):
        from metrics import render
        return render.compression_row(self)


class SessionScope(Scope):
    """One device connection; exports to ``metrics.session`` (connect_pre_roll).

    Constructed at session teardown with the connection's accept/hello/first-
    audio perf_counter anchors in meta; standalone (never the registry active
    scope, which is per-turn).
    """

    def __init__(self, *, user_id: str, device_id: str, session_id: str):
        super().__init__(user_id=user_id, persona_id=0)
        self.meta["device_id"] = device_id
        self.meta["session_id"] = session_id

    def ch_row(self):
        from metrics import render
        return render.session_row(self)
