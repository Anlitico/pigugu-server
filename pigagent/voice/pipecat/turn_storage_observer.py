"""TurnStorage observer — builds the per-turn record from pipeline frames.

Sits after the UserTurnProcessor and before the AgentGateway. It:
- routes upstream mic audio into TWO buffers by the assistant's speaking flag:
  - ``_turn_buf``: audio while the assistant is NOT speaking → becomes this
    turn's ``input.wav`` (user utterance + surrounding silence),
  - ``_gap_buf``: audio while the assistant IS speaking → becomes the PREVIOUS
    turn's ``listen.wav`` (the reply-playback / AEC-probe window),
  routing on ``state.client_is_speaking`` (set by the TTS bridge in real time)
  because pipecat reorders frames: audio reaches the observer before the
  device listen/start and UserStartedSpeakingFrame, so a window pointer keyed
  to those signals would pin the window to reply-echo audio and collapse the
  listen window,
- on ``UserStoppedSpeakingFrame`` closes the previous turn's storage (its
  listen.wav) and opens this turn's storage (input.wav), deferred to the next
  boundary so the reply-period listen audio is included,
- marks the ``stt_final`` telemetry segment on the first transcript,
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

# Upper bound on each per-turn audio buffer. A turn that never ends (no
# vad_silence, STT never finalizes) must not grow memory forever — beyond
# this, the oldest audio is dropped (old connection.py cut asr_audio at TTS
# start). 10 minutes of 16k mono int16 ≈ 19 MB.
_AUDIO_BUF_CAP = 10 * 60 * SAMPLE_RATE * 2

# How long the close task will wait for the TTS bridge to finish marking the
# previous turn's storage (tts_complete + telemetry) before committing it with
# whatever is there. Almost always returns immediately (the reply ended before
# the next turn started); only a barge-in that races the TTS task actually
# waits. Runs off the pipeline's critical path.
_FINALIZE_TIMEOUT_SECS = 5.0


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
        # The previous turn's storage, held open until the next turn boundary
        # so its listen.wav (the reply-playback period) can be attached. The
        # TTS bridge marks it (stt/tts); the observer closes + commits it.
        self._open_storage: TurnStorage | None = None
        # Audio routing: non-reply mic audio accumulates in ``_turn_buf`` and
        # becomes the next turn's input.wav; reply-period mic audio (echo) in
        # ``_gap_buf`` becomes the current turn's listen.wav. Routed by
        # ``state.client_is_speaking``, which the TTS bridge updates in real
        # time and is NOT reordered like the frame signals.
        self._turn_buf = bytearray()
        self._gap_buf = bytearray()
        # Wall-clock + Silero-flag position where ``_turn_buf`` actually began
        # (captured lazily on its first byte after a stop, so reply-echo and
        # pre-reply latency are excluded). Feeds audio_start_ms + the bounded
        # voice_chunk_flags slice.
        self._turn_start_ms = int(time.time() * 1000)
        self._voice_chunk_start = 0
        # Captured at each turn stop; applied to the next turn's window on the
        # first turn audio (or listen/start) so the start is never moved past
        # audio that already flowed. Overwritten at reply-END so the next turn's
        # window re-opens from the reply boundary (its input.wav has no reply
        # echo).
        self._pending_turn_start_ms: int | None = int(time.time() * 1000)
        self._pending_voice_chunk_start: int | None = 0
        self._last_speaking = False
        # True from a user turn start (listen/start / UserStartedSpeaking) until
        # its stop. Guards the reply-START edge: pre-reply listening audio is
        # only moved to the gap when no user turn is in flight (a roast inject
        # may set client_is_speaking mid-utterance — that audio stays put).
        self._user_turn_active = False
        self._saw_text = False
        self._stt_final_marked = False
        if state.interims is None:
            state.interims = InterimBuffer()

    # ── frame handling ────────────────────────────────────────────────

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            if self._enabled:
                self._route_audio(frame.audio)
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

    def _route_audio(self, pcm: bytes):
        """Route one upstream mic frame: reply-period audio (the assistant is
        speaking) goes to the gap buffer (→ the current turn's listen.wav /
        AEC probe); all other audio goes to the turn buffer (→ the next
        input.wav).

        On the reply-START edge the pre-reply listening audio already in the
        turn buffer (TTFT latency silence, mic streaming before the reply) is
        moved to the gap so it lands BEFORE the echo and is not folded into the
        next input.wav; on the reply-END edge the next turn's window re-opens
        from the reply boundary, so its voice_segments exclude the echo."""
        speaking = self._state.client_is_speaking
        if speaking and not self._last_speaking:
            # Reply started: the turn buffer holds pre-reply listening audio
            # (TTFT latency silence) — move it into the gap so it lands BEFORE
            # the echo. Skip when a user turn is in flight (a roast inject may
            # set client_is_speaking mid-utterance): that audio belongs to the
            # in-progress user turn, not the previous turn's listen.
            if self._turn_buf and not self._user_turn_active:
                self._gap_buf.extend(self._turn_buf)
                self._turn_buf = bytearray()
        elif self._last_speaking and not speaking:
            # Reply ended: the next turn's input window begins here (post-reply),
            # so the Silero-flags slice starts past the echo.
            self._pending_turn_start_ms = int(time.time() * 1000)
            self._pending_voice_chunk_start = self._voice_flag_len()
        self._last_speaking = speaking
        if speaking:
            self._append_gap(pcm)
        else:
            self._append_turn(pcm)

    def _append_turn(self, pcm: bytes):
        """Append non-reply audio (→ the next input.wav), dropping the oldest
        beyond the cap so a never-ending turn cannot grow memory without
        bound."""
        if not self._turn_buf:
            self._begin_turn_window()
        self._turn_buf.extend(pcm)
        excess = len(self._turn_buf) - _AUDIO_BUF_CAP
        if excess > 0:
            del self._turn_buf[:excess]

    def _append_gap(self, pcm: bytes):
        """Append reply-period audio (→ the current listen.wav), capped."""
        self._gap_buf.extend(pcm)
        excess = len(self._gap_buf) - _AUDIO_BUF_CAP
        if excess > 0:
            del self._gap_buf[:excess]

    def _begin_turn_window(self):
        """Apply the pending window start captured at the last turn stop.

        Called on the first turn audio after a stop (and on device
        ``listen/start``). Does nothing once ``_turn_buf`` holds audio — a late
        listen/start must NOT move the boundary past audio that already beat it.
        """
        if self._turn_buf:
            return
        if self._pending_turn_start_ms is not None:
            self._turn_start_ms = self._pending_turn_start_ms
            self._voice_chunk_start = self._pending_voice_chunk_start or 0
        self._saw_text = False
        self._stt_final_marked = False

    # ── turn lifecycle ────────────────────────────────────────────────

    def _on_user_started(self):
        # New VAD turn — telemetry only (audio is routed by client_is_speaking,
        # not by VAD start).
        self._user_turn_active = True
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
        # Close the PREVIOUS turn's storage in a background task: its listen.wav
        # is the reply-period audio accumulated in _gap_buf since its reply
        # played. The task awaits the TTS mark (so the committed row carries the
        # reply) but never blocks this pipeline frame.
        if self._open_storage is not None:
            storage_to_close = self._open_storage
            self._open_storage = None
            storage_to_close.set_listen_pcm(bytes(self._gap_buf))
            asyncio.ensure_future(self._close_storage(storage_to_close))
        # Reset buffers for the next turn, capturing the pending window start
        # at this boundary.
        self._gap_buf = bytearray()
        turn_pcm = bytes(self._turn_buf)
        self._turn_buf = bytearray()
        self._pending_turn_start_ms = int(time.time() * 1000)
        self._pending_voice_chunk_start = self._voice_flag_len()
        self._user_turn_active = False
        if not turn_pcm:
            # Empty turn (spurious vad_silence with no audio): there is no
            # input window — point the slices at the stop so voice_segments and
            # audio_start_ms don't leak the previous turn's audio into this row.
            self._voice_chunk_start = self._pending_voice_chunk_start
            self._turn_start_ms = self._pending_turn_start_ms
        if not self._enabled:
            self._state.turn_type = "follow_up"
            return
        storage = self._make_storage()
        if storage is None:
            self._state.turn_type = "follow_up"
            return
        storage.set_user_pcm(turn_pcm)
        self._state.turn_storage = storage
        self._open_storage = storage
        # The next turn defaults back to follow_up (a wake word re-arms it).
        self._state.turn_type = "follow_up"
        # A device_playback_ms ack belongs to exactly one turn — reset so the
        # next turn cannot inherit the previous turn's value when it gets no
        # validated ack of its own.
        self._state.device_playback_ms = 0
        if not self._saw_text:
            # No TranscriptionFrame → the gateway emits no turn frame → the
            # TTS bridge never finalizes. Mark it here; commit is still
            # deferred to the next boundary so the listen window is included.
            storage.mark_stt_final("")
            storage.mark_tts_complete("", ok=False, truncated_reason="no_stt")
            storage.mark_finalized()
        # Reset per-turn transcript flags for the next turn here too, so an
        # empty turn that never runs _begin_turn_window does not inherit this
        # turn's _saw_text (which would skip the no_stt mark above next round).
        self._saw_text = False
        self._stt_final_marked = False

    async def _close_storage(self, storage: TurnStorage) -> None:
        """Background close: wait (bounded) for the TTS bridge to finish
        marking the turn, then commit. Runs off the pipeline's critical path
        and survives cancellation (a torn-down close still commits — the
        storage was already detached from ``_open_storage``, so nothing else
        will)."""
        try:
            await asyncio.wait_for(
                storage.finalized_event.wait(),
                timeout=_FINALIZE_TIMEOUT_SECS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"[PiguguTurnStorageObserver] finalize timeout "
                f"turn_id={storage.turn_id}"
            )
        except asyncio.CancelledError:
            pass  # loop teardown — fall through and commit anyway
        try:
            asyncio.ensure_future(storage.commit())
        except RuntimeError:
            # Loop is closing — the commit cannot be scheduled; surface it so
            # the turn is at least not silently dropped.
            logger.warning(
                f"[PiguguTurnStorageObserver] commit not scheduled (loop closing) "
                f"turn_id={storage.turn_id}"
            )

    async def finalize_session(self) -> None:
        """Session ended: close the open turn's storage with the trailing
        audio — its reply echo (``_gap_buf``) plus any non-reply audio that
        never became a turn (``_turn_buf``: partial utterance / post-reply
        silence) — and commit. Waits (bounded) for the TTS bridge to finish
        marking (like ``_close_storage``); only on timeout is the disconnect
        fallback applied, so a reply actually generated before the disconnect
        still lands in the row."""
        storage = self._open_storage
        if storage is None:
            return
        self._open_storage = None
        if not storage.finalized:
            try:
                await asyncio.wait_for(
                    storage.finalized_event.wait(),
                    timeout=_FINALIZE_TIMEOUT_SECS,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"[PiguguTurnStorageObserver] finalize timeout at session end "
                    f"turn_id={storage.turn_id}"
                )
        if not storage.finalized:
            # TTS bridge never finalized (disconnect before the reply) — mark
            # the disconnect so the user's audio is still preserved.
            storage.mark_stt_final("")
            storage.mark_tts_complete("", ok=False, truncated_reason="disconnect")
            storage.mark_finalized()
        tail = bytes(self._gap_buf) + bytes(self._turn_buf)
        storage.set_listen_pcm(tail)
        asyncio.ensure_future(storage.commit())

    # ── device ack (tts_played) ───────────────────────────────────────

    async def _on_device_message(self, msg: dict):
        if msg.get("type") != "listen":
            return
        state = msg.get("state")
        if state == "start":
            # Device started listening for a new utterance — apply the pending
            # window start (no-op if turn audio already flowed).
            self._begin_turn_window()
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

    def _voice_chunk_flags_slice(self, start_idx: int, end_idx: int) -> list[bool]:
        """Bounded slice of the Silero chunk flags for ONE turn's input.wav,
        translated past the Silero 10-minute trim. Bounded [start:end] so the
        deferred commit (which runs at the NEXT turn boundary) never leaks a
        later turn's voice into this turn's voice_segments."""
        flags = getattr(self._vad, "_voice_chunk_flags", None)
        if not flags:
            return []
        trimmed = getattr(self._vad, "_voice_chunk_flags_trimmed", 0)
        lo = max(0, start_idx - trimmed)
        hi = min(max(0, end_idx - trimmed), len(flags))
        return list(flags[lo:hi])

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
        # The bounded flags window: from this turn's first input audio (which
        # excludes reply-echo, routed to _gap_buf) to this turn's stop.
        start_idx = self._voice_chunk_start
        end_idx = self._voice_flag_len()
        return TurnStorage(
            turn_id=turn_id,
            session_id=self._session_id,
            turn_idx=self._turn_idx,
            device_id=self._client_id,
            user_id=self._user_id,
            persona_id=self._persona_id,
            utc_start_ms=utc_start_ms,
            audio_start_ms=self._turn_start_ms,
            s3_bucket=bucket,
            s3_prefix=prefix,
            clickhouse_dsn=ch_dsn,
            clickhouse_table=ch_table,
            interims=self._state.interims,
            voice_chunk_flags_slice=lambda: self._voice_chunk_flags_slice(start_idx, end_idx),
            turn_type=self._state.turn_type,
        )
