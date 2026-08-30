"""Raw-websocket input/output transports for the pigugu device protocol.

These subclass Pipecat's transport base classes and own the device
``websockets`` connection directly (multi-device on one port — the stock
``SingleClientWebsocketServerTransport`` binds a whole port per client).

The input transport runs the read loop: it deserializes wire messages, replies
to the hello handshake, and pushes audio/control frames downstream. The output
transport serializes downstream audio/control frames back to the wire.
"""

from __future__ import annotations

import json
from typing import Any

from loguru import logger
from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    OutputAudioRawFrame,
    StartFrame,
)
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import TransportParams

from voice.pipecat.pigugu_serializer import (
    PiguguFrameSerializer,
    PiguguMessageFrame,
    PiguguOpusFrame,
    PiguguOutputMessageFrame,
)
from voice.pipecat.state import PiguguTurnState


class PiguguInputTransport(BaseInputTransport):
    """Owns the device WS read loop; deserializes frames into the pipeline.

    Handles the hello handshake inline (sets ``raw_pcm`` from the device's
    audio_params, stashes hw_id/persona_id on the shared state, and replies),
    then forwards every other message downstream — ``InputAudioRawFrame`` for
    Opus/PCM packets, ``PiguguMessageFrame`` for listen/abort control messages.
    """

    def __init__(
        self,
        websocket: Any,
        serializer: PiguguFrameSerializer,
        params: TransportParams,
        *,
        session_id: str,
        state: PiguguTurnState | None = None,
        on_disconnect: Any = None,
        **kwargs,
    ):
        super().__init__(params, **kwargs)
        self._ws = websocket
        self._serializer = serializer
        self._session_id = session_id
        self._state = state or PiguguTurnState()
        self._on_disconnect = on_disconnect
        self._receive_task = None

    async def start(self, frame: StartFrame):
        await super().start(frame)
        await self.set_transport_ready(frame)
        self._receive_task = self.create_task(self._receive_messages())

    async def _stop_receive(self):
        if self._receive_task:
            await self.cancel_task(self._receive_task)
            self._receive_task = None

    async def stop(self, frame: EndFrame):
        await super().stop(frame)
        await self._stop_receive()

    async def cancel(self, frame: Frame):
        await super().cancel(frame)
        await self._stop_receive()

    async def _receive_messages(self):
        try:
            async for message in self._ws:
                frame = await self._serializer.deserialize(message)
                if frame is None:
                    continue
                if isinstance(frame, PiguguMessageFrame):
                    await self._handle_control(frame)
                elif isinstance(frame, InputAudioRawFrame):
                    # No VAD/processing here — the bridges handle that (M2).
                    # push_audio_frame() needs the base audio task's _audio_in_queue,
                    # which we don't run; push downstream directly instead.
                    await self.push_frame(frame)
        except Exception as e:
            logger.error(f"[PiguguInputTransport] read loop error: {type(e).__name__}: {e}")
        finally:
            # Client gone — tell the session to end the worker gracefully.
            # (An EndFrame pushed from mid-pipeline does not stop the worker;
            # it must be queued through the worker itself, hence the callback.)
            if self._on_disconnect:
                await self._on_disconnect()

    async def _handle_control(self, frame: PiguguMessageFrame):
        msg = frame.message
        mtype = msg.get("type", "")
        if mtype == "hello":
            audio_params = msg.get("audio_params", {})
            self._serializer.raw_pcm = audio_params.get("format", "opus") == "pcm"
            try:
                self._state.persona_id = int(msg.get("persona_id", 1) or 1)
            except (TypeError, ValueError):
                pass
            self._state.hw_id = str(msg.get("hw_id", ""))
            logger.info(
                f"[PiguguInputTransport] hello session={self._session_id} "
                f"persona={self._state.persona_id} hw_id={self._state.hw_id!r} "
                f"raw_pcm={self._serializer.raw_pcm}"
            )
            await self._ws.send(json.dumps(self._hello_reply()))
        else:
            await self.push_frame(frame)

    def _hello_reply(self) -> dict:
        return {
            "type": "hello",
            "transport": "websocket",
            "session_id": self._session_id,
            "audio_params": {
                "format": "pcm" if self._serializer.raw_pcm else "opus",
                "sample_rate": self._serializer.sample_rate,
                "channels": self._serializer.channels,
                "frame_duration": self._serializer.frame_duration_ms,
            },
        }


class PiguguOutputTransport(BaseOutputTransport):
    """Serializes downstream frames to the pigugu wire and sends them."""

    def __init__(
        self,
        websocket: Any,
        serializer: PiguguFrameSerializer,
        params: TransportParams,
        **kwargs,
    ):
        super().__init__(params, **kwargs)
        self._ws = websocket
        self._serializer = serializer

    async def start(self, frame: StartFrame):
        await super().start(frame)
        # Registers the default media sender (destination None) so audio
        # frames route to write_audio_frame.
        await self.set_transport_ready(frame)

    async def process_frame(self, frame: Frame, direction: Any):
        if isinstance(frame, (PiguguOutputMessageFrame, PiguguOpusFrame, OutputAudioRawFrame)):
            # Serialize and send directly. The base routes audio through a
            # MediaSender that buffers/resamples/chunks at 10ms granularity —
            # unnecessary for a raw device socket (the TTS bridge owns pacing).
            payload = await self._serializer.serialize(frame)
            if payload:
                await self._send(payload)
        else:
            await super().process_frame(frame, direction)

    async def _send(self, payload: str | bytes) -> bool:
        try:
            await self._ws.send(payload)
            return True
        except Exception as e:
            logger.warning(f"[PiguguOutputTransport] send failed: {type(e).__name__}: {e}")
            return False
