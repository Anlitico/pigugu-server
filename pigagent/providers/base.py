"""Provider base classes — abstract interfaces for pluggable components."""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum, auto
from typing import Any


class InterfaceType(Enum):
    """ASR interface type — mirrors official."""
    STREAM = auto()   # WebSocket streaming
    SPEECH = auto()   # REST batch (non-streaming fallback)


class VADProvider(ABC):
    """Voice Activity Detection — per-frame speech probability."""

    @abstractmethod
    def is_vad(self, conn: Any, pcm_frame: bytes) -> bool:
        """Return True if ``pcm_frame`` contains speech.

        ``conn`` is the ConnectionHandler instance (VAD state stored on it).
        """
        ...

    def release_conn_resources(self, conn: Any) -> None:
        """Optional: clean up per-connection VAD state."""
        pass


class STTProvider(ABC):
    """Speech-to-Text — convert PCM audio to transcribed text.

    Supports two modes:
      - Streaming (interface_type = STREAM): ``receive_audio`` per frame
      - Batch    (interface_type = SPEECH): ``transcribe(pcm)`` one-shot
    """

    interface_type: InterfaceType = InterfaceType.SPEECH

    @abstractmethod
    async def transcribe(self, pcm: bytes) -> str:
        """Batch: convert raw PCM (16 kHz, s16le, mono) to text.

        Returns empty string if no speech detected or error.
        """
        ...

    async def receive_audio(self, conn: Any, pcm: bytes, have_voice: bool) -> None:
        """Streaming: feed one PCM frame. Override for streaming providers."""
        raise NotImplementedError("Streaming not supported by this provider")

    async def handle_voice_stop(self, conn: Any, audio_data: list[bytes]) -> None:
        """Called when voice stops — finalize recognition. Batch fallback."""
        ...

    async def open_audio_channels(self, conn: Any) -> None:
        """Called when audio streaming starts. Override for streaming init."""
        pass

    async def close_audio_channels(self) -> None:
        """Called when connection closes. Clean up streaming resources."""
        pass


class TTSProvider(ABC):
    """Text-to-Speech — convert text to Opus-encoded audio frames."""

    @abstractmethod
    async def synthesize(self, text: str, raw_pcm: bool = False) -> list[bytes]:
        """Convert text to a list of Opus-encoded (or raw PCM) binary frames.

        Each frame is one 60 ms chunk at 16 kHz.
        Set ``raw_pcm=True`` to skip Opus encoding.
        """
        ...


class LLMProvider(ABC):
    """Large-Language Model — generate assistant reply stream from dialogue."""

    @abstractmethod
    async def response_async(
        self,
        session_id: str,
        dialogue: list[dict],
        **kwargs: Any,
    ):
        """Async generator yielding text tokens.

        ``dialogue`` is a list of ``{"role": ..., "content": ...}`` dicts.
        """
        ...
