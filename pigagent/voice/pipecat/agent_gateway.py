"""Turn → agent handoff: merges the turn transcript, emits one user-turn frame.

Grows out of M2's aggregator: on ``UserStoppedSpeakingFrame`` the accumulated
``TranscriptionFrame`` texts become ONE ``PiguguUserTurnFrame`` handed to the
TTS bridge (which runs the LLM + TTS). ``on_turn`` is kept for tests and
observers. This is the 6be1be41 fix made visible — one user utterance,
however many Deepgram ``is_final`` chunks it produced, is ONE turn.

Deliberately does NOT reset on ``UserStartedSpeakingFrame``: that broadcast is
async and can race the transcription frames. Every turn ends with a
``UserStoppedSpeakingFrame`` (stop strategy or watchdog), which is the only
place the buffer is cleared — so turn boundaries stay correct regardless of
frame ordering.
"""

from __future__ import annotations

from loguru import logger
from pipecat.frames.frames import (
    Frame,
    TranscriptionFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from voice.pipecat.pigugu_serializer import PiguguOutputMessageFrame, PiguguUserTurnFrame


class PiguguAgentGateway(FrameProcessor):
    """Accumulate turn transcript; emit one ``PiguguUserTurnFrame`` on turn end."""

    def __init__(self, on_turn=None, **kwargs):
        super().__init__(**kwargs)
        self._text_parts: list[str] = []
        self._on_turn = on_turn

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame):
            self._text_parts.append(frame.text)
        elif isinstance(frame, UserStoppedSpeakingFrame):
            merged = " ".join(p for p in self._text_parts if p).strip()
            self._text_parts = []
            if merged:
                logger.info(f"[PiguguAgentGateway] TURN: '{merged}'")
                if self._on_turn:
                    await self._on_turn(merged)
                # stt message first (device shows the user's text), then hand
                # the turn to the agent — parity with old connection.py:939.
                await self.push_frame(
                    PiguguOutputMessageFrame(message={"type": "stt", "text": merged})
                )
                await self.push_frame(PiguguUserTurnFrame(text=merged))
        # Pass everything downstream (audio dies at the output transport, which
        # only sends Opus / control frames).
        await self.push_frame(frame, direction)
