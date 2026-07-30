"""Deepgram STT provider — REST API for batch transcription."""

from __future__ import annotations

import os
from typing import Any

import aiohttp
from loguru import logger

from providers.base import STTProvider


class DeepgramSTT(STTProvider):
    """Speech-to-text via Deepgram Nova-3 REST API.

    Parameters
    ----------
    api_key : str
        Deepgram API key (reads ``DEEPGRAM_API_KEY`` env var if empty).
    model : str
        Model name (default ``nova-3``).
    language : str
        Language code (default ``en``).
    sample_rate : int
        Audio sample rate in Hz (default 16000).
    """

    def __init__(
        self,
        api_key: str = "",
        model: str = "nova-3",
        language: str = "en",
        sample_rate: int = 16000,
    ):
        self._api_key = api_key or os.getenv("DEEPGRAM_API_KEY", "")
        self._model = model
        self._language = language
        self._sample_rate = sample_rate
        self._http: aiohttp.ClientSession | None = None

    async def _ensure_http(self) -> aiohttp.ClientSession:
        if self._http is None:
            timeout = aiohttp.ClientTimeout(total=30, connect=10)
            self._http = aiohttp.ClientSession(timeout=timeout)
        return self._http

    async def transcribe(self, pcm: bytes) -> str:
        """Send PCM (16kHz s16le mono) to Deepgram, return transcript."""
        if not self._api_key:
            logger.error("[Deepgram] DEEPGRAM_API_KEY not set")
            return ""
        if len(pcm) < 1600:
            logger.debug(f"[Deepgram] Audio too short: {len(pcm)} bytes")
            return ""

        url = (
            f"https://api.deepgram.com/v1/listen"
            f"?model={self._model}"
            f"&language={self._language}"
            f"&encoding=linear16"
            f"&sample_rate={self._sample_rate}"
        )
        try:
            http = await self._ensure_http()
            async with http.post(
                url,
                data=pcm,
                headers={
                    "Authorization": f"Token {self._api_key}",
                    "Content-Type": "audio/x-raw",
                },
            ) as resp:
                if resp.status != 200:
                    err_body = await resp.text()
                    logger.error(
                        f"[Deepgram] Error {resp.status} body={err_body[:300]}"
                    )
                    return ""
                result = await resp.json()
                channel = result.get("results", {}).get("channels", [{}])[0]
                alternatives = channel.get("alternatives", [{}])
                return alternatives[0].get("transcript", "")
        except Exception:
            logger.exception("[Deepgram] API call failed")
            return ""

    async def close(self) -> None:
        if self._http:
            await self._http.close()
            self._http = None
