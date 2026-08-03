"""Deepgram STT provider — WebSocket streaming + REST batch fallback."""

from __future__ import annotations

import asyncio
import os
from typing import Any

import aiohttp
from deepgram import DeepgramClient, LiveTranscriptionEvents, LiveOptions
from loguru import logger

from providers.base import InterfaceType, STTProvider


class DeepgramSTT(STTProvider):
    """Deepgram Nova-3 STT — streaming via WebSocket, batch via REST."""

    interface_type = InterfaceType.STREAM

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

    # ── Streaming (receive_audio) ─────────────────────────────────────

    async def open_audio_channels(self, conn: Any) -> None:
        if not self._api_key:
            logger.error("[Deepgram] DEEPGRAM_API_KEY not set")
            return

        client = DeepgramClient(self._api_key)
        conn.dg_connection = client.listen.websocket.v("1")

        # Accumulate results on conn for official pattern
        conn.deepgram_final = asyncio.Event()
        conn.deepgram_transcript = ""

        def on_open(_, open, **kwargs):
            logger.debug("[Deepgram] STT WS opened")
            conn.deepgram_final.clear()

        def on_message(_, result, **kwargs):
            try:
                alt = result.channel.alternatives[0]
                text = alt.transcript
                if not text:
                    return
                conn.deepgram_transcript = text
                if result.is_final:
                    logger.info(f"[Deepgram] FINAL: '{text[:120]}'")
                    conn.deepgram_final.set()
                else:
                    logger.debug(f"[Deepgram] interim: '{text[:80]}'")
            except Exception:
                logger.exception("[Deepgram] on_message error")

        def on_error(_, error, **kwargs):
            logger.error(f"[Deepgram] STT WS error: {error}")

        conn.dg_connection.on(LiveTranscriptionEvents.Open, on_open)
        conn.dg_connection.on(LiveTranscriptionEvents.Transcript, on_message)
        conn.dg_connection.on(LiveTranscriptionEvents.Error, on_error)

        options = LiveOptions(
            model=self._model,
            language=self._language,
            smart_format=True,
            encoding="linear16",
            sample_rate=self._sample_rate,
            channels=1,
            interim_results=True,
            punctuate=True,
            endpointing=300,
        )

        if not conn.dg_connection.start(options):
            logger.error("[Deepgram] Failed to start Deepgram WS")
        else:
            logger.info("[Deepgram] Streaming STT started")

    async def close_audio_channels(self) -> None:
        logger.debug("[Deepgram] Closing STT WS")

    async def receive_audio(self, conn: Any, pcm: bytes, have_voice: bool) -> None:
        if not hasattr(conn, "dg_connection"):
            return
        try:
            conn.dg_connection.send(pcm)
        except Exception:
            logger.exception("[Deepgram] send failed")

    async def handle_voice_stop(self, conn: Any, audio_data: list[bytes]) -> None:
        """Voice stopped — wait for Deepgram final result."""
        try:
            await asyncio.wait_for(conn.deepgram_final.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            logger.warning("[Deepgram] Timeout waiting for final result")

    # ── Batch fallback (REST) ─────────────────────────────────────────

    async def _ensure_http(self) -> aiohttp.ClientSession:
        if self._http is None:
            timeout = aiohttp.ClientTimeout(total=30, connect=10)
            self._http = aiohttp.ClientSession(timeout=timeout)
        return self._http

    async def transcribe(self, pcm: bytes) -> str:
        """Batch: send entire PCM to Deepgram REST API."""
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
                    logger.error(f"[Deepgram] Error {resp.status} body={err_body[:300]}")
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
