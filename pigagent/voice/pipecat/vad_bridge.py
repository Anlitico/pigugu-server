"""VAD bridge — tracks device + Silero voice signals for the turn sidecar.

No turn-detection frames are emitted: turn START is transcription-driven
(MinWordsUserTurnStartStrategy on the first STT final) and turn END is
Deepgram's utterance-end. The device's own AFE VAD stays device-internal.

This bridge only:
- runs Silero on every audio frame to fill ``_voice_chunk_flags`` (the
  per-turn ``voice_segments[]`` sidecar),
- records device ``listen/vad_silence`` timing marks for diagnostics,
- classifies wake-word turns (``listen/detect`` -> turn_type + wake_word).
"""

from __future__ import annotations

import time
from typing import Any

from loguru import logger
from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from voice.pipecat.pigugu_serializer import PiguguMessageFrame
from voice.pipecat.state import PiguguTurnState

# How long a single voiced chunk keeps ``state.vad_voice_active`` true. This
# bridges the GAPS INSIDE a real utterance -- measured on real captures,
# ``last_is_voice`` is true for only 24-59% of a user utterance's chunks -- it
# is not an echo-suppression window. Echo never sets it at all.
_VOICE_HOLD_MS = 500


class PiguguVadBridge(FrameProcessor):
    """Tracks device + Silero voice signals for the turn sidecar (no turn frames)."""

    def __init__(self, vad: Any | None, *, state: PiguguTurnState | None = None, **kwargs):
        super().__init__(**kwargs)
        self.vad = vad
        self._state = state or PiguguTurnState()
        # The echo gate's precondition: without a VAD there is no verdict to
        # gate on, and the gate must stay inert rather than block every
        # barge-in.
        self._state.vad_wired = vad is not None
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
        # perf_counter of the last voiced chunk (0.0 = nobody has spoken yet).
        self._last_voice_pc = 0.0

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
        self.vad.is_vad(self, pcm)  # populates _voice_chunk_flags + voice state
        # VAD emits NO start/stop frames: turn-start is MinWords
        # (transcription-driven) and turn-end is Deepgram's utterance-end.
        # Emitting a VADUserStartedSpeakingFrame here would set the turn
        # controller's _user_speaking (and the stop strategy's
        # _vad_user_speaking) with no matching stop frame — the VAD stop was
        # removed because Silero can't tell a mid-thought pause from a finished
        # utterance. Those stuck-True flags then block the 5s turn-stop
        # watchdog, so a lost utterance-end would hang the turn forever.
        # client_have_voice / client_voice_stop stay tracked for the
        # voice_segments sidecar but emit no frames.

        # Publish the instantaneous verdict (held) for the STT bridge's echo
        # gate. ``last_is_voice`` is already past the provider's energy gate,
        # and it leads ``client_have_voice`` -- which additionally needs 500ms
        # of unbroken voice -- by ~0.5s. Both are zero-trigger on residual
        # echo, so the faster one costs no barge-in latency.
        if hasattr(self, "last_is_voice"):
            now = time.perf_counter()
            if self.last_is_voice:
                self._last_voice_pc = now
            self._state.vad_voice_active = (
                (now - self._last_voice_pc) * 1000.0 <= _VOICE_HOLD_MS
            )
        elif self._state.vad_wired:
            # The provider never published a verdict: it raised on the chunk
            # (onnx swallows and returns) or does not publish this attribute at
            # all. Keying the gate on a signal that never arrives would
            # silently kill barge-in for the session, so disarm it and say so.
            self._state.vad_wired = False
            logger.warning(
                "[PiguguVadBridge] VAD published no last_is_voice verdict; "
                "echo gate disabled for this session"
            )

        if self.client_voice_stop:
            self.client_voice_stop = False

    async def _on_control(self, frame: PiguguMessageFrame):
        msg = frame.message
        if msg.get("type") != "listen":
            return
        state = msg.get("state")
        logger.info(f"[PiguguVadBridge] listen state={state} client={self.session_id}")
        if state == "start":
            # Device opened its mic. Do NOT emit VADUserStartedSpeakingFrame:
            # turn-start is MinWords (transcription), and a start frame here
            # would set _user_speaking with no matching stop — blocking the
            # turn-stop watchdog (see _on_audio comment).
            pass
        elif state == "detect":
            # Wake word: classify the following turn (the gateway PREPENDS the
            # wake word to that turn's text) and reset VAD voice state.
            self._state.turn_type = "wake_word"
            # The firmware sends the wake-word text here; the gateway puts it
            # back at the head of the turn's text, since the firmware never
            # streamed that audio (CONFIG_SEND_WAKE_WORD_DATA is off).
            self._state.wake_word = str(msg.get("text", "") or "")
            self.client_have_voice = False
            self.client_voice_stop = False
            logger.info(f"[PiguguVadBridge] wake word client={self.session_id}")
        elif state == "vad_silence":
            # Firmware is aligned to xiaozhi: VAD is device-internal only and
            # no longer drives server turn logic. Keep recording the timing
            # marks for diagnostics/backward compat (old firmware still sends
            # this), but do NOT push a turn stop — turn-end is Deepgram's
            # utterance-end only.
            self._on_vad_silence(msg)
        elif state == "stop":
            # Client stopped listening — reset VAD/voice-tracking state
            # (old _handle_listen / _reset_audio_states).
            self.client_have_voice = False
            self.client_voice_stop = False

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
