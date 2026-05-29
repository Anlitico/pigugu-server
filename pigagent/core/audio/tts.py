"""TTS provider factory — LiveKit Inference path (Cartesia Sonic)."""

from __future__ import annotations

import os
from typing import Any, Optional

from livekit.agents.inference import TTS
from loguru import logger


def create_tts(
    model: str = "cartesia/sonic-3",
    *,
    language: str = "en",
    voice: str = "9783574a-63f4-46bf-b56b-928eb52d3140",
    speed: Optional[float] = None,
    emotion: Optional[list[str]] = None,
    volume: Optional[float] = None,
    sample_rate: int = 24000,
    max_buffer_delay_ms: int = 100,
    api_key: Optional[str] = None,
    base_url: str = "https://api.cartesia.ai",
) -> TTS:
    """Create a Cartesia TTS instance via LiveKit Inference.

    Uses the inference path (not the plugin) to access Cartesia's
    max_buffer_delay_ms parameter for low-latency streaming.
    """
    api_key = api_key or os.getenv("CARTESIA_API_KEY")
    if not api_key:
        raise ValueError("CARTESIA_API_KEY is required for Cartesia TTS")

    extra: dict[str, Any] = {"max_buffer_delay_ms": max_buffer_delay_ms}
    if speed is not None:
        extra["speed"] = speed
    if emotion:
        extra["emotion"] = ",".join(emotion) if isinstance(emotion, list) else emotion
    if volume is not None:
        extra["volume"] = volume

    tts = TTS(
        model=model,
        voice=voice,
        language=language,
        sample_rate=sample_rate,
        api_key=api_key,
        base_url=base_url,
        extra_kwargs=extra,
    )

    logger.info(
        f"Created Cartesia TTS (inference): model={model} voice={voice} "
        f"language={language} max_buffer_delay_ms={max_buffer_delay_ms} "
        f"speed={speed} emotion={emotion} volume={volume}"
    )
    return tts
