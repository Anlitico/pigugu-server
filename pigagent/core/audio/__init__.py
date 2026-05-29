# pigagent/core/audio/__init__.py
"""Core audio infrastructure  -  STT and TTS providers."""

from .stt import create_stt, STTProvider
from .tts import create_tts

__all__ = [
    "create_stt", "STTProvider",
    "create_tts",
]
