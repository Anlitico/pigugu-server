"""M1 loopback stub — echo user audio back as TTS (temporary, replaced by the
real VAD/STT/LLM/TTS chain in M2/M3).

The device only plays audio after ``tts/start``, so this processor drives the
protocol: on ``listen/start`` it opens TTS (echo), on ``listen/vad_silence`` /
``listen/stop`` it closes it.
"""

from __future__ import annotations

from pipecat.frames.frames import Frame, InputAudioRawFrame, OutputAudioRawFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from voice.pipecat.pigugu_serializer import PiguguMessageFrame, PiguguOutputMessageFrame


class PiguguEchoProcessor(FrameProcessor):
    """Temporary M1 processor: echo the device's mic back through its speaker."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        if isinstance(frame, InputAudioRawFrame):
            # Echo the user's mic back as TTS output.
            await self.push_frame(
                OutputAudioRawFrame(
                    audio=frame.audio, sample_rate=frame.sample_rate, num_channels=frame.num_channels
                )
            )
            return
        if isinstance(frame, PiguguMessageFrame):
            msg = frame.message
            if msg.get("type") == "listen":
                state = msg.get("state")
                if state == "start":
                    await self.push_frame(
                        PiguguOutputMessageFrame(
                            message={"type": "tts", "state": "start", "sentence_id": 1}
                        )
                    )
                elif state in ("vad_silence", "stop"):
                    await self.push_frame(
                        PiguguOutputMessageFrame(message={"type": "tts", "state": "stop"})
                    )
        # FrameProcessor base does not forward frames downstream — every
        # processor must pass non-consumed frames (StartFrame, EndFrame, ...)
        # along explicitly.
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)
