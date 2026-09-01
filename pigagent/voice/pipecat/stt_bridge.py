"""STT bridge — feeds our Deepgram STT and emits Pipecat transcription frames.

This processor *is* the ``conn`` object Deepgram's callbacks expect: it owns
``_on_stt_final`` / ``_on_stt_interim`` (dispatched from Deepgram's background
thread via ``run_coroutine_threadsafe``) and ``client_is_speaking`` (set by the
TTS bridge while the assistant is talking).

The 6be1be41 fix lives here: Deepgram ``is_final`` becomes a plain
``TranscriptionFrame`` that the turn layer accumulates — it no longer defines a
turn boundary. Interim speech while the assistant is speaking triggers a barge-in.
"""

from __future__ import annotations

import asyncio
from typing import Any

import numpy as np
from loguru import logger
from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    ProposedUserStoppedSpeakingFrame,
    TranscriptionFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from voice.pipecat.state import PiguguTurnState

_INPUT_GAIN = 10.0


class PiguguSttBridge(FrameProcessor):
    """Feeds audio to Deepgram and translates results into Pipecat frames."""

    def __init__(
        self,
        stt: Any,
        *,
        state: PiguguTurnState | None = None,
        context_loader: Any = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.stt = stt
        self._state = state or PiguguTurnState()
        self._dg_final_buffer: list[str] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        # Latest conversation context (the agent's last reply), fed to the STT
        # when it supports_context. Produced by the TTS bridge via push_context.
        self._last_context: str = ""
        # Optional async callable returning the user's last agent reply from
        # persisted history. Fired once when the stream opens and we have no
        # in-session context yet — so a reconnect starts with the last reply as
        # STT context instead of a blank decoder.
        self._context_loader = context_loader

    # Deepgram callback contract (read by deepgram.py on_message):
    # client_is_speaking is owned by the TTS bridge via the shared turn state.
    @property
    def client_is_speaking(self) -> bool:
        return self._state.client_is_speaking

    async def setup(self, setup: Any):
        await super().setup(setup)
        self._loop = asyncio.get_running_loop()

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        if isinstance(frame, InputAudioRawFrame):
            await self._feed(frame.audio)
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)

    async def _feed(self, pcm: bytes):
        if self.stt is None:
            return
        gained = self._gain(pcm)
        # Default (no is_open): treat as not-open so open_audio_channels runs
        # (idempotent in real providers, a no-op in the FakeSTT test doubles).
        is_open = getattr(self.stt, "is_open", lambda _c: False)
        if not is_open(self):
            await self.stt.open_audio_channels(self)
            # Seed context on open, BEFORE the first audio frame is sent, so the
            # decoder has the agent's last reply from the start. Prefers the
            # in-session context (push_context); falls back to the persisted
            # history loader so a reconnect doesn't start blank.
            if not self._last_context and self._context_loader is not None:
                try:
                    # Bound the DB query: this runs on the hot first-frame path
                    # before audio flows, so a hung PG must not stall the pipeline.
                    ctx = await asyncio.wait_for(self._context_loader(), timeout=2.0)
                    if ctx:
                        trim = getattr(self.stt, "trim_context", lambda t: t)
                        self._last_context = trim(str(ctx).strip())
                except Exception:
                    logger.warning("[SttBridge] context_loader failed", exc_info=True)
            if self._last_context:
                await self.push_context(self._last_context)
        await self.stt.receive_audio(self, gained, True)

    @staticmethod
    def _gain(pcm: bytes) -> bytes:
        if len(pcm) < 2:
            return pcm
        arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        arr *= _INPUT_GAIN
        np.clip(arr, -32768, 32767, out=arr)
        return arr.astype(np.int16).tobytes()

    # ── Deepgram thread callbacks (run_coroutine_threadsafe → this loop) ──

    async def _on_stt_final(self, text: str) -> None:
        if self._loop is None:
            return
        text = text.strip()
        if not text:
            return
        # stt_final telemetry is marked by the TurnStorageObserver on the
        # pipeline task (it owns the turn context; the Deepgram thread here
        # would see a None contextvar and the mark would be a no-op).
        await self.push_frame(TranscriptionFrame(text=text, user_id="", timestamp=_now()))

    async def _on_stt_interim(self, text: str) -> None:
        if self._loop is None:
            return
        text = text.strip()
        if not text:
            return
        if self._state.interims is not None:
            self._state.interims.record(text)
        # Barge-in is handled by MinWordsUserTurnStartStrategy (official pipecat
        # pattern): a turn start over the bot broadcasts the interruption.
        await self.push_frame(
            InterimTranscriptionFrame(text=text, user_id="", timestamp=_now())
        )

    async def _on_utterance_end(self) -> None:
        """The STT provider's definitive end-of-utterance marker.

        The frame type depends on the provider's ``turn_end_signal``:
          - "vad" (Deepgram): VADUserStoppedSpeakingFrame — the speech-timeout
            stop strategy's stt wait short-circuits immediately (stop_secs=0,
            transcript already final).
          - "external" (AssemblyAI): ProposedUserStoppedSpeakingFrame — the
            ExternalUserTurnStopStrategy decides, with no inactivity fallback,
            so mid-sentence pauses never split a turn.
        A turn-stop that does not depend on server VAD — it fires on the STT
        model's own endpointing, so it stays reliable through the wake-word
        audio burst, a noisy room, and absent device vad_silence.
        """
        if self._loop is None:
            return
        if getattr(self.stt, "turn_end_signal", "vad") == "external":
            await self.push_frame(ProposedUserStoppedSpeakingFrame())
        else:
            await self.push_frame(VADUserStoppedSpeakingFrame(stop_secs=0.0))

    async def push_context(self, context: str) -> None:
        """Forward fresh conversation context to a context-aware STT provider.

        The producer is the TTS bridge (agent's last spoken reply). Providers
        without ``supports_context`` (e.g. Deepgram) are untouched — the whole
        routing is inert for them.
        """
        if self.stt is None or not getattr(self.stt, "supports_context", False):
            return
        text = (context or "").strip()
        if not text:
            return
        trim = getattr(self.stt, "trim_context", lambda t: t)
        self._last_context = trim(text)
        update = getattr(self.stt, "update_context", None)
        if update is not None:
            try:
                # conn = this bridge — the context belongs to this session.
                await update(self, self._last_context)
            except Exception:
                logger.exception("[SttBridge] update_context failed")

    async def cleanup(self) -> None:
        """Close the provider's per-connection stream at session end."""
        await super().cleanup()
        close = getattr(self.stt, "close_connection", None) if self.stt is not None else None
        if close is not None:
            await close(self)


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
