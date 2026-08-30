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

    def __init__(self, on_turn=None, *, state=None, **kwargs):
        super().__init__(**kwargs)
        self._text_parts: list[str] = []
        self._on_turn = on_turn
        self._state = state
        # Turn context captured from the FIRST transcript of the turn. The
        # observer (upstream) resets state.turn_type to "follow_up" on
        # UserStoppedSpeakingFrame, so by the time this processor merges, the
        # wake_word classification is already gone — capture it while the
        # transcripts are still flowing (mid-turn, turn_type is still set).
        self._captured_turn_type: str = "follow_up"
        self._captured_wake_word: str = ""

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame):
            if not self._text_parts and self._state is not None:
                self._captured_turn_type = self._state.turn_type
                self._captured_wake_word = self._state.wake_word or ""
            self._text_parts.append(frame.text)
        elif isinstance(frame, UserStoppedSpeakingFrame):
            merged = " ".join(p for p in self._text_parts if p).strip()
            merged = self._strip_wake_word(merged)
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

    def _strip_wake_word(self, text: str) -> str:
        """Remove the leading wake word from the wake-word turn's transcript.

        The firmware streams the wake-word audio to Deepgram, so the first
        turn's text starts with e.g. "Alexa? nice to meet you..." and the LLM
        (Pigugu, not Alexa) reads that as the user addressing another
        assistant. Stripped once from the merged start, so multiple is_final
        chunks cannot duplicate the surviving words.
        """
        if self._captured_turn_type != "wake_word":
            return text
        ww = self._captured_wake_word.strip()
        if not ww:
            return text
        t = text.strip()
        if not t.lower().startswith(ww.lower()):
            return text
        rest = t[len(ww):].lstrip(" ,.?!，。？！:：;；'\"")
        if not rest:
            return ""
        return rest
