"""pigugu wire-protocol serializer for Pipecat.

Protocol v1 (what the pigugu firmware actually speaks): raw Opus packets
(60ms / 16kHz / mono) with no framing header, plus a small JSON
control-message set. See pigugu-firmware websocket_protocol.cc.

Device→server::
    bytes  -> Opus packet -> InputAudioRawFrame
    str    -> JSON -> PiguguMessageFrame   (hello / listen / abort)

Server→device::
    OutputAudioRawFrame -> Opus packet -> bytes
    PiguguOutputMessageFrame -> JSON -> str   (hello / stt / tts)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import opuslib
from loguru import logger
from pipecat.frames.frames import Frame, InputAudioRawFrame, OutputAudioRawFrame
from pipecat.serializers.base_serializer import FrameSerializer

SAMPLE_RATE = 16000
CHANNELS = 1
FRAME_DURATION_MS = 60
FRAME_SAMPLES = FRAME_DURATION_MS * SAMPLE_RATE // 1000  # 960


@dataclass
class PiguguMessageFrame(Frame):
    """A device→server JSON control message (hello / listen / abort)."""

    message: dict[str, Any]


@dataclass
class PiguguOutputMessageFrame(Frame):
    """A server→device JSON control message (hello / stt / tts)."""

    message: dict[str, Any]


@dataclass
class PiguguOpusFrame(Frame):
    """A single 60ms Opus packet ready for the wire (from the TTS bridge).

    Cartesia's stream_audio already emits Opus, so re-encoding PCM would be
    wasteful; the serializer forwards these bytes unchanged.
    """

    audio: bytes


@dataclass
class PiguguUserTurnFrame(Frame):
    """A completed user utterance, ready for the agent (LLM + TTS)."""

    text: str


class PiguguFrameSerializer(FrameSerializer):
    """Convert Pipecat frames to/from the pigugu v1 wire protocol.

    ``raw_pcm`` (browser test client) toggles Opus off on both directions —
    the current server accepts a ``hello.audio_params.format == "pcm"``
    client that streams raw 16kHz PCM.
    """

    def __init__(
        self,
        params: FrameSerializer.InputParams | None = None,
        *,
        sample_rate: int = SAMPLE_RATE,
        channels: int = CHANNELS,
        frame_duration_ms: int = FRAME_DURATION_MS,
    ):
        super().__init__(params)
        self.sample_rate = sample_rate
        self.channels = channels
        self.frame_duration_ms = frame_duration_ms
        self._frame_samples = frame_duration_ms * sample_rate // 1000
        self.raw_pcm = False
        self._decoder: opuslib.Decoder | None = None
        self._encoder: opuslib.Encoder | None = None
        # Incremental encode carry: TTS PCM may arrive in arbitrary chunk
        # sizes, the device needs fixed 60ms Opus frames.
        self._enc_carry = bytearray()

    # ── codec (lazy) ─────────────────────────────────────────────────

    def _get_decoder(self) -> opuslib.Decoder:
        if self._decoder is None:
            self._decoder = opuslib.Decoder(self.sample_rate, self.channels)
        return self._decoder

    def _get_encoder(self) -> opuslib.Encoder:
        if self._encoder is None:
            self._encoder = opuslib.Encoder(self.sample_rate, self.channels, "voip")
        return self._encoder

    # ── deserialize: device → server ─────────────────────────────────

    async def deserialize(self, data: str | bytes) -> Frame | None:
        if isinstance(data, str):
            try:
                return PiguguMessageFrame(message=json.loads(data))
            except (json.JSONDecodeError, TypeError):
                logger.warning(f"[PiguguSerializer] bad JSON: {data[:120]!r}")
                return None
        pcm = data if self.raw_pcm else self._decode_opus(data)
        if not pcm:
            return None
        return InputAudioRawFrame(
            audio=pcm, sample_rate=self.sample_rate, num_channels=self.channels
        )

    def _decode_opus(self, data: bytes) -> bytes | None:
        try:
            return self._get_decoder().decode(data, self._frame_samples)
        except Exception:
            return None

    # ── serialize: server → device ───────────────────────────────────

    async def serialize(self, frame: Frame) -> str | bytes | None:
        if isinstance(frame, PiguguOutputMessageFrame):
            return json.dumps(frame.message, ensure_ascii=False)
        if isinstance(frame, PiguguOpusFrame):
            # Already a 60ms Opus packet (TTS bridge): pass through untouched.
            return frame.audio
        if isinstance(frame, OutputAudioRawFrame):
            if self.raw_pcm:
                return bytes(frame.audio)
            return self._encode_pcm(frame.audio)
        return None

    def _encode_pcm(self, pcm: bytes) -> bytes | None:
        """Buffer PCM, emit a 60ms Opus frame for every complete window.

        Returns the oldest complete frame, or None while less than 60ms of
        new audio has accumulated (the remainder stays buffered).
        """
        if not pcm:
            return None
        self._enc_carry.extend(pcm)
        frame_bytes = self._frame_samples * self.channels * 2
        if len(self._enc_carry) < frame_bytes:
            return None
        chunk = bytes(self._enc_carry[:frame_bytes])
        del self._enc_carry[:frame_bytes]
        try:
            return self._get_encoder().encode(chunk, self._frame_samples)
        except Exception:
            return None

    def flush(self) -> bytes | None:
        """Encode and clear any buffered partial frame (padded), if any."""
        frame_bytes = self._frame_samples * self.channels * 2
        if not self._enc_carry:
            return None
        chunk = bytes(self._enc_carry)
        self._enc_carry.clear()
        if len(chunk) < frame_bytes:
            chunk = chunk + b"\x00" * (frame_bytes - len(chunk))
        try:
            return self._get_encoder().encode(chunk, self._frame_samples)
        except Exception:
            return None
