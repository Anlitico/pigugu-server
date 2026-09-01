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
    # How this provider signals end-of-turn. "vad" (default) → the bridge maps
    # utterance_end to a VADUserStoppedSpeakingFrame, handled by a speech-timeout
    # stop strategy. "external" → the provider's own semantic endpointing maps to
    # a ProposedUserStoppedSpeakingFrame, handled by ExternalUserTurnStopStrategy
    # (no inactivity fallback — mid-sentence pauses don't split turns).
    turn_end_signal: str = "vad"
    # Whether this provider consumes conversation context (e.g. the agent's
    # last spoken reply) to sharpen decoding. False → the framework's context
    # routing is fully inert, so non-context providers are untouched. The
    # pipeline feeds context via ``update_context`` (and seeds it when the
    # stream opens); the producer side is the TTS bridge.
    supports_context: bool = False
    # Cap applied by the framework's ``trim_context`` before a context value is
    # sent (keep the TRAILING part — the portion closest to the user's next
    # turn carries the most signal).
    max_context_chars: int = 1750

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

    def is_open(self, conn: Any) -> bool:
        """True if the streaming connection is open (idempotency guard for the
        bridge's per-frame ``open_audio_channels`` call). Providers set
        ``conn._stt_open`` when they open their stream."""
        return bool(getattr(conn, "_stt_open", False))

    async def close_audio_channels(self) -> None:
        """Called when connection closes. Clean up streaming resources."""
        pass

    async def close_connection(self, conn: Any) -> None:
        """Per-connection teardown (cancel tasks, close sockets). Called by the
        bridge's ``cleanup()`` at session end. Default no-op."""
        pass

    def trim_context(self, text: str) -> str:
        """Trim a context value to ``max_context_chars``, keeping the tail.

        The tail is the portion closest to the user's next turn — e.g. the
        question the agent just asked — and carries the most signal, so we keep
        it rather than the head when truncating.
        """
        text = text or ""
        if len(text) <= self.max_context_chars:
            return text
        return text[-self.max_context_chars:]

    async def update_context(self, conn: Any, context: str) -> None:
        """Push fresh conversation context into the decoder (no-op default).

        Only context-aware providers (``supports_context=True``) implement this.
        ``conn`` is the per-session connection the context belongs to — the
        provider instance may be shared across sessions, so context state must
        be kept on ``conn``, not on ``self``. Called mid-conversation whenever
        the producer emits new context (the agent's last spoken reply), and
        seeded when the stream opens.
        """
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
