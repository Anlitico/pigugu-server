"""Cartesia TTS provider — text → Opus-encoded audio frames."""

from __future__ import annotations

import os

import aiohttp
from loguru import logger

from providers.base import TTSProvider


class CartesiaTTS(TTSProvider):
    """Text-to-speech via Cartesia REST + SSE API.

    Parameters
    ----------
    api_key : str
        Cartesia API key (reads ``CARTESIA_API_KEY`` env var if empty).
    voice_id : str
        Voice ID (reads ``CARTESIA_TTS_VOICE`` env var if empty).
    model_id : str
        Model ID (default ``sonic-3.5``).
    sample_rate : int
        Output audio sample rate in Hz (default 16000).
    """

    URL = "https://api.cartesia.ai/tts/bytes"

    def __init__(
        self,
        api_key: str = "",
        voice_id: str = "",
        model_id: str = "sonic-3.5",
        sample_rate: int = 16000,
    ):
        self._api_key = api_key or os.getenv("CARTESIA_API_KEY", "")
        self._voice_id = voice_id or os.getenv(
            "CARTESIA_TTS_VOICE", "9783574a-63f4-46bf-b56b-928eb52d3140"
        )
        self._model_id = model_id
        self._sample_rate = sample_rate
        self._http: aiohttp.ClientSession | None = None

    async def _ensure_http(self) -> aiohttp.ClientSession:
        if self._http is None:
            timeout = aiohttp.ClientTimeout(total=30, connect=10)
            self._http = aiohttp.ClientSession(timeout=timeout)
        return self._http

    async def synthesize(self, text: str, raw_pcm: bool = False) -> list[bytes]:
        """Convert text to a list of Opus-encoded (or raw PCM) binary frames.

        Each frame is one 60 ms chunk at the configured sample rate.
        Set ``raw_pcm=True`` to skip Opus encoding (for browser testing).
        """
        if not self._api_key:
            logger.error("[Cartesia] CARTESIA_API_KEY not set")
            return []
        if not text.strip():
            return []

        payload = {
            "model_id": self._model_id,
            "transcript": text,
            "voice": {"mode": "id", "id": self._voice_id},
            "output_format": {
                "container": "raw",
                "encoding": "pcm_s16le",
                "sample_rate": self._sample_rate,
            },
            "language": "en",
        }
        try:
            http = await self._ensure_http()
            async with http.post(
                self.URL,
                json=payload,
                headers={
                    "X-API-Key": self._api_key,
                    "Cartesia-Version": "2024-06-10",
                },
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.error(
                        f"[Cartesia] Error {resp.status} body={body[:200]}"
                    )
                    return []
                pcm = bytearray()
                async for data in resp.content.iter_chunked(4096):
                    pcm.extend(data)
        except Exception:
            logger.exception("[Cartesia] API call failed")
            return []

        if len(pcm) == 0:
            logger.warning("[Cartesia] Empty PCM returned")
            return []

        if raw_pcm:
            # Return raw PCM directly (browser testing)
            return [bytes(pcm)]

        # Encode PCM → Opus (60ms frames at the configured sample rate)
        return _opus_encode_chunks(
            bytes(pcm),
            sample_rate=self._sample_rate,
            channels=1,
            frame_duration_ms=60,
        )

    async def close(self) -> None:
        if self._http:
            await self._http.close()
            self._http = None


# ── Opus encoding helper ──────────────────────────────────────────────

def _opus_encode_chunks(
    pcm: bytes, sample_rate: int = 16000, channels: int = 1, frame_duration_ms: int = 60
) -> list[bytes]:
    """Encode raw PCM (s16le) to a list of Opus frames."""
    try:
        import opuslib  # pyright: ignore[reportMissingImports]

        encoder = opuslib.Encoder(sample_rate, channels, "voip")
        frames: list[bytes] = []
        frame_samples = frame_duration_ms * sample_rate // 1000
        frame_bytes = frame_samples * channels * 2
        pos = 0
        while pos + frame_bytes <= len(pcm):
            frame = pcm[pos : pos + frame_bytes]
            try:
                encoded = encoder.encode(bytes(frame), frame_samples)
                frames.append(encoded)
            except Exception:
                pass
            pos += frame_bytes
        return frames
    except ImportError:
        logger.warning("[Cartesia] opuslib not available — returning raw PCM")
        return [pcm]
