"""Provider base classes — abstract interfaces for pluggable components."""

from abc import ABC, abstractmethod
from typing import Any, Generator, Optional


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
    """Speech-to-Text — convert PCM audio to transcribed text."""

    @abstractmethod
    async def transcribe(self, pcm: bytes) -> str:
        """Convert raw PCM (16 kHz, s16le, mono) to text.

        Returns empty string if no speech detected or error.
        """
        ...


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
    def response(
        self,
        session_id: str,
        dialogue: list[dict],
        **kwargs: Any,
    ) -> Generator[str, None, None]:
        """Synchronous generator yielding text tokens.

        ``dialogue`` is a list of ``{"role": ..., "content": ...}`` dicts.
        """
        ...
