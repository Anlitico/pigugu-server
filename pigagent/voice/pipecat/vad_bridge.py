"""VAD bridge — turns voice signals into Pipecat turn-detection frames.

Two sources:
- **Device signals (authoritative)**: ``listen/start`` -> turn starts,
  ``listen/vad_silence`` -> user stopped speaking. This is the firmware's
  local AFE VAD — low latency and proven.
- **Server Silero (fallback + segments)**: runs on every audio frame to fill
  ``_voice_chunk_flags`` (for the per-turn ``voice_segments[]`` sidecar) and
  to emit VAD start/stop frames when the device signal is absent (e.g. a
  browser raw-PCM client). Pipecat's UserTurnController dedupes consecutive
  starts/stops, so both sources firing is harmless.
"""

from __future__ import annotations

import time
from typing import Any

from loguru import logger
from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from voice.pipecat.pigugu_serializer import PiguguMessageFrame
from voice.pipecat.state import PiguguTurnState

# VAD stop silence window used when the device doesn't report its own
# (server-Silero fallback). Matches the Silero provider's min_silence_duration_ms.
_SILERO_STOP_SECS = 0.7

# Server-VAD suppression after a wake word, so the wake-word audio does not
# trigger a phantom turn start (old connection.py just_woken_up / _resume_vad).
_WAKE_VAD_SUPPRESS_SECS = 2.0


class PiguguVadBridge(FrameProcessor):
    """Emits VADUserStarted/StoppedSpeakingFrame from device + Silero signals."""

    def __init__(self, vad: Any | None, *, state: PiguguTurnState | None = None, **kwargs):
        super().__init__(**kwargs)
        self.vad = vad
        self._state = state or PiguguTurnState()
        # Silero per-connection state lives on this instance (is_vad stores on conn).
        # ``client_audio_buffer`` is the Silero chunk accumulator — the migration
        # from connection.py must keep the conn contract (onnx.py:74 extends it,
        # 77-79 drain 512-sample chunks). Missing it crashed every audio frame and
        # killed server-side VAD turn detection (no TTS).
        self.client_have_voice = False
        self.client_voice_stop = False
        self.client_listen_mode = "auto"
        self.client_audio_buffer = bytearray()
        self.session_id = "?"
        # monotonic deadline during which server-VAD turn starts are suppressed.
        self._suppress_until = 0.0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        if isinstance(frame, InputAudioRawFrame):
            await self._on_audio(frame.audio)
        elif isinstance(frame, PiguguMessageFrame):
            await self._on_control(frame)
        # Pass everything downstream (audio continues to the STT bridge).
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)

    async def _on_audio(self, pcm: bytes):
        if self.vad is None:
            return
        prev_have_voice = self.client_have_voice
        self.vad.is_vad(self, pcm)  # populates _voice_chunk_flags + voice state
        # Wake-word suppression gates only the server-VAD START frame. The
        # voice-state bookkeeping must keep running: Silero's stop detection
        # reads client_have_voice on the NEXT frame to arm the silence timer,
        # so zeroing it here (the old override) made the wake-word utterance
        # never end and swallowed later speech into the same turn.
        in_wake_suppress = time.monotonic() < self._suppress_until
        if self.client_have_voice and not prev_have_voice and not in_wake_suppress:
            await self.push_frame(VADUserStartedSpeakingFrame())
        if self.client_voice_stop:
            await self.push_frame(VADUserStoppedSpeakingFrame(stop_secs=_SILERO_STOP_SECS))
            self.client_voice_stop = False

    async def _on_control(self, frame: PiguguMessageFrame):
        msg = frame.message
        if msg.get("type") != "listen":
            return
        state = msg.get("state")
        if state == "start":
            await self.push_frame(VADUserStartedSpeakingFrame())
        elif state == "detect":
            # Wake word: classify the following turn, suppress server-VAD
            # starts for a moment, and reset VAD state (old _on_detect).
            self._state.turn_type = "wake_word"
            # The firmware sends the wake-word text here; the gateway strips
            # it from the first turn's transcript so the LLM does not see it.
            self._state.wake_word = str(msg.get("text", "") or "")
            self._suppress_until = time.monotonic() + _WAKE_VAD_SUPPRESS_SECS
            self.client_have_voice = False
            self.client_voice_stop = False
            logger.info(f"[PiguguVadBridge] wake word client={self.session_id}")
        elif state == "vad_silence":
            self._on_vad_silence(msg)
            await self.push_frame(VADUserStoppedSpeakingFrame(stop_secs=_SILERO_STOP_SECS))
        elif state == "stop":
            # Client stopped listening — reset VAD/voice-tracking state
            # (old _handle_listen / _reset_audio_states).
            self.client_have_voice = False
            self.client_voice_stop = False
            self._suppress_until = 0.0

    def _on_vad_silence(self, msg: dict):
        """Record the server-received-vad + reconstructed vad_end values
        (ported from connection.py ``_on_vad_silence``). The device sends a
        duration, not a timestamp — the server rebuilds the end-of-utterance
        on its own perf_counter clock so every latency segment shares a base.

        Only STORES the raw perf_counter values on state — the observer
        applies them to the correct turn dict at turn end (this processor
        cannot assume its contextvar matches the turn being closed).
        """
        user_stop_age_ms = int(msg.get("user_stop_age_ms", 0) or 0)
        server_received_at = time.perf_counter()
        if user_stop_age_ms > 0:
            self._state.vad_end_mark = server_received_at - user_stop_age_ms / 1000.0
        else:
            self._state.vad_end_mark = server_received_at
        self._state.server_received_vad_at = server_received_at
        logger.info(
            f"[PiguguVadBridge] vad_silence user_stop_age_ms={user_stop_age_ms} "
            f"client={self.session_id}"
        )
