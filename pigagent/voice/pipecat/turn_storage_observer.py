"""TurnStorage observer — builds the per-turn record from pipeline frames.

Sits after the UserTurnProcessor and before the AgentGateway. It:
- accumulates the user's PCM over the VAD turn (window = turn start → stop),
  capped so a never-ending turn cannot grow memory without bound,
- marks the ``stt_final`` telemetry segment on the first transcript,
- on ``UserStoppedSpeakingFrame`` builds a ``TurnStorage`` and hands it to the
  TTS bridge via ``state.turn_storage`` for finalization (tts text / tts PCM /
  telemetry) and commit,
- validates the device ``tts_played`` ack against the current sentence id and
  records ``device_playback_ms``,
- commits a no-STT turn immediately (no turn frame will ever be emitted).

Storage lives in ``voice/storage.py`` unchanged; this observer is the new
pipeline-side producer that replaces the ConnectionHandler's per-turn glue.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Callable
from urllib.parse import quote

from loguru import logger
from metrics.turn import TelemetryCollector
from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from voice.interims import InterimBuffer
from voice.pipecat.pigugu_serializer import PiguguMessageFrame, SAMPLE_RATE
from voice.pipecat.state import PiguguTurnState
from voice.pipecat.telemetry import ensure_turn_context
from voice.storage import TurnStorage, is_turn_storage_enabled

# Upper bound on the session audio ring. A turn that never ends (no
# vad_silence, STT never finalizes) must not grow memory forever — beyond
# this, the oldest audio is dropped (old connection.py cut asr_audio at TTS
# start). 10 minutes of 16k mono int16 ≈ 19 MB.
_AUDIO_BUF_CAP = 10 * 60 * SAMPLE_RATE * 2


class PiguguTurnStorageObserver(FrameProcessor):
    """Per-turn audio/transcript accumulator → TurnStorage."""

    def __init__(
        self,
        vad: Any,
        state: PiguguTurnState,
        *,
        session_id: str,
        client_id: str,
        user_id: str,
        persona_id: int = 1,
        **kwargs,
    ):
        super().__init__(**kwargs)
        # ``vad`` is the VAD BRIDGE instance (not the provider): silero's
        # is_vad stores per-connection flags on the conn, which is the bridge.
        self._vad = vad
        self._state = state
        self._session_id = session_id
        self._client_id = client_id
        self._user_id = user_id or client_id
        self._persona_id = persona_id
        self._enabled = is_turn_storage_enabled()
        self._turn_idx = 0
        # Session-wide PCM ring: audio is appended unconditionally and the
        # per-turn window is a SLICE [window_start:], taken at turn end. The
        # boundary is captured ONCE per turn (see _capture_window_start) — the
        # device's listen/start and the first audio frame are both ordering-
        # safe, whereas UserStartedSpeakingFrame is broadcast asynchronously
        # and may arrive before OR after the audio frames.
        self._audio_buf = bytearray()
        self._window_start: int | None = None
        self._window_start_ms = 0
        self._voice_chunk_start = 0
        self._saw_text = False
        self._stt_final_marked = False
        if state.interims is None:
            state.interims = InterimBuffer()

    # ── frame handling ────────────────────────────────────────────────

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            self._capture_window_start()
            if self._enabled:
                self._append_audio(frame.audio)
        elif isinstance(frame, TranscriptionFrame):
            if frame.text and frame.text.strip():
                self._saw_text = True
                # stt_final closes the STT latency segment. Marked here (on the
                # pipeline task, which owns the turn context) rather than in the
                # Deepgram callback thread, where the contextvar is None.
                if not self._stt_final_marked:
                    self._stt_final_marked = True
                    TelemetryCollector.mark("stt_final")
        elif isinstance(frame, PiguguMessageFrame):
            await self._on_device_message(frame.message)
        elif isinstance(frame, UserStartedSpeakingFrame):
            self._on_user_started()
        elif isinstance(frame, UserStoppedSpeakingFrame):
            await self._on_user_stopped()
        # Pass everything downstream (audio/transcripts continue to the agent).
        await self.push_frame(frame, direction)

    def _append_audio(self, pcm: bytes):
        """Append user PCM, dropping the oldest beyond the cap so a never-
        ending turn cannot grow memory without bound."""
        self._audio_buf.extend(pcm)
        excess = len(self._audio_buf) - _AUDIO_BUF_CAP
        if excess > 0:
            del self._audio_buf[:excess]
            if self._window_start is not None:
                self._window_start = max(0, self._window_start - excess)

    # ── turn lifecycle ────────────────────────────────────────────────

    def _capture_window_start(self):
        """Establish the current turn's audio-window boundary once.

        Called from listen/start, the first audio frame, and the (possibly
        late) UserStartedSpeakingFrame. Whichever fires first wins; a late
        start event must NOT move the boundary past audio that already beat
        it.
        """
        if self._window_start is not None:
            return
        self._window_start = len(self._audio_buf)
        self._window_start_ms = int(time.time() * 1000)
        self._voice_chunk_start = self._voice_flag_len()
        self._saw_text = False
        self._stt_final_marked = False

    def _on_user_started(self):
        # New VAD turn — telemetry + window boundary (captured once).
        TelemetryCollector.start_turn(
            user_id=self._user_id,
            persona_id=self._persona_id,
        )
        # Share the turn dict across processors: Pipecat runs each FrameProcessor
        # in its own task with an isolated contextvars copy.
        from metrics.turn import _current_var as _turn_var

        self._state.active_turn = _turn_var.get()
        TelemetryCollector.set_meta("turn_phase", self._state.turn_type)
        TelemetryCollector.mark("vad_start")
        self._capture_window_start()

    async def _on_user_stopped(self):
        # Apply the device vad_silence marks to THIS turn (the one ending):
        # the vad bridge stored the raw perf_counter values, and applying them
        # here (in the observer's task, which owns this turn's context) gives
        # correct attribution regardless of cross-processor async ordering.
        if self._state.server_received_vad_at is not None:
            ensure_turn_context(self._state)
            TelemetryCollector.set_mark(
                "server_received_vad_at", self._state.server_received_vad_at
            )
            TelemetryCollector.set_meta("vad_end_source", "reconstructed_from_age_ms")
            if self._state.vad_end_mark is not None:
                TelemetryCollector.set_mark("vad_end", self._state.vad_end_mark)
            self._state.server_received_vad_at = None
            self._state.vad_end_mark = None
        # Freeze this turn's window and compact the ring regardless of storage
        # enablement, so idle audio never accumulates across turns.
        start = self._window_start if self._window_start is not None else 0
        del self._audio_buf[:start]
        self._window_start = None
        if not self._enabled:
            self._state.turn_type = "follow_up"
            return
        storage = self._make_storage()
        if storage is None:
            self._state.turn_type = "follow_up"
            return
        # After the compact, the remaining buffer IS this turn's window.
        storage.set_user_pcm(bytes(self._audio_buf))
        self._state.turn_storage = storage
        # The next turn defaults back to follow_up (a wake word re-arms it).
        self._state.turn_type = "follow_up"
        # A device_playback_ms ack belongs to exactly one turn — reset so the
        # next turn cannot inherit the previous turn's value when it gets no
        # validated ack of its own.
        self._state.device_playback_ms = 0
        if not self._saw_text:
            # No TranscriptionFrame → the gateway emits no turn frame → the
            # TTS bridge never finalizes. Commit now as a no-STT turn.
            storage.mark_stt_final("")
            storage.mark_tts_complete("", ok=False, truncated_reason="no_stt")
            self._state.turn_storage = None
            asyncio.ensure_future(storage.commit())

    # ── device ack (tts_played) ───────────────────────────────────────

    async def _on_device_message(self, msg: dict):
        if msg.get("type") != "listen":
            return
        state = msg.get("state")
        if state == "start":
            # Device started listening — the user-audio window begins here.
            self._capture_window_start()
            return
        if state != "tts_played":
            return
        ms = int(msg.get("device_playback_ms", 0) or 0)
        if ms <= 0:
            return
        # Validate the sentence id so a late ack for a previous turn is not
        # attached to this one (short replies are the classic mis-attribution).
        played_sid = msg.get("sentence_id")
        try:
            played_sid = int(played_sid)
        except (TypeError, ValueError):
            played_sid = None
        cur_sid = self._state.current_sentence_id
        # Attach only when a TTS turn is actually playing AND the sentence ids
        # match. cur_sid==0 means the reply already ended — a late ack must
        # not be attributed to the next turn (old _flush_late_tts_played kept
        # a separate late marker; here the safest outcome is to drop it).
        if played_sid is not None and (not cur_sid or played_sid != cur_sid):
            logger.info(
                f"[PiguguTurnStorageObserver] late tts_played sid={played_sid} "
                f"cur={cur_sid} — dropped"
            )
            return
        self._state.device_playback_ms = ms

    # ── storage construction ──────────────────────────────────────────

    def _voice_flag_len(self) -> int:
        if self._vad is None:
            return 0
        flags = getattr(self._vad, "_voice_chunk_flags", None)
        return len(flags) if flags else 0

    def _voice_chunk_flags_slice(self, start_idx: int) -> list[bool]:
        """Per-turn slice of the Silero chunk flags, translated past the
        Silero 10-minute trim (same logic as connection.py)."""
        flags = getattr(self._vad, "_voice_chunk_flags", None)
        if not flags:
            return []
        trimmed = getattr(self._vad, "_voice_chunk_flags_trimmed", 0)
        return list(flags[max(0, start_idx - trimmed):])

    def _make_storage(self) -> TurnStorage | None:
        bucket = os.getenv("AUDIO_S3_BUCKET", "").strip()
        prefix = os.getenv("AUDIO_S3_PREFIX", "voice-turns").strip()
        ch_host = os.getenv("CLICKHOUSE_HOST", "clickhouse").strip()
        ch_port = os.getenv("CLICKHOUSE_PORT", "9000").strip()
        ch_user = os.getenv("CLICKHOUSE_USER", "default").strip()
        ch_db = os.getenv("CLICKHOUSE_DATABASE", "voice").strip()
        ch_table = os.getenv("CLICKHOUSE_TABLE", f"{ch_db}.turns").strip()
        ch_password = os.getenv("CLICKHOUSE_PASSWORD", "")
        if not (bucket and ch_host and ch_password):
            logger.warning(
                f"[PiguguTurnStorageObserver] turn storage misconfigured "
                f"bucket={bucket!r} ch_host={ch_host!r} ch_password_set={bool(ch_password)}"
            )
            return None
        ch_dsn = (
            f"clickhouse://{quote(ch_user, safe='')}:{quote(ch_password, safe='')}"
            f"@{ch_host}:{ch_port}/{ch_db}"
        )
        self._turn_idx += 1
        utc_start_ms = int(time.time() * 1000)
        turn_id = f"{utc_start_ms}_{self._session_id}_{self._turn_idx:04d}"
        captured_start = self._voice_chunk_start
        return TurnStorage(
            turn_id=turn_id,
            session_id=self._session_id,
            turn_idx=self._turn_idx,
            device_id=self._client_id,
            user_id=self._user_id,
            persona_id=self._persona_id,
            utc_start_ms=utc_start_ms,
            audio_start_ms=self._window_start_ms,
            s3_bucket=bucket,
            s3_prefix=prefix,
            clickhouse_dsn=ch_dsn,
            clickhouse_table=ch_table,
            interims=self._state.interims,
            voice_chunk_flags_slice=lambda: self._voice_chunk_flags_slice(captured_start),
            turn_type=self._state.turn_type,
        )
