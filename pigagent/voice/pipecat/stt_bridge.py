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
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from providers.stt.deepgram import _should_barge_in
from voice.pipecat.state import PiguguTurnState

_INPUT_GAIN = 10.0


class PiguguSttBridge(FrameProcessor):
    """Feeds audio to Deepgram and translates results into Pipecat frames."""

    def __init__(self, stt: Any, *, state: PiguguTurnState | None = None, **kwargs):
        super().__init__(**kwargs)
        self.stt = stt
        self._state = state or PiguguTurnState()
        self._dg_final_buffer: list[str] = []
        self._loop: asyncio.AbstractEventLoop | None = None

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
        if not hasattr(self, "_dg_socket"):
            await self.stt.open_audio_channels(self)
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
        await self.push_frame(
            InterimTranscriptionFrame(text=text, user_id="", timestamp=_now())
        )
        if self.client_is_speaking and _should_barge_in(self, text):
            logger.info(f"[PiguguSttBridge] barge-in: '{text[:60]}'")
            await self.broadcast_interruption()


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
