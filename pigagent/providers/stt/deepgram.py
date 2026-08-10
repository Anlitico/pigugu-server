"""Deepgram STT provider — WebSocket streaming (v7 SDK) + REST batch fallback."""

from __future__ import annotations

import asyncio
import os
from typing import Any

import aiohttp
from deepgram import DeepgramClient
from deepgram.core.events import EventType
from loguru import logger

from providers.base import InterfaceType, STTProvider


class DeepgramSTT(STTProvider):
    """Deepgram Nova-3 STT — streaming via WebSocket v1, batch via REST."""

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

    # ── Streaming (v7 SDK: client.listen.v1.connect()) ────────────────

    async def open_audio_channels(self, conn: Any) -> None:
        """Open Deepgram streaming once — stays open for the session.
        Deepgram handles endpointing internally (1000ms silence → final).
        Final results delivered via conn._on_stt_final(text)."""
        if not self._api_key:
            logger.error("[Deepgram] DEEPGRAM_API_KEY not set")
            return

        import threading
        client = DeepgramClient(api_key=self._api_key)
        conn.deepgram_transcript = ""
        conn._dg_final_buffer: list[str] = []  # accumulate is_final until speech_final

        def on_message(result):
            try:
                if not hasattr(result, "channel"):
                    return
                text = result.channel.alternatives[0].transcript
                if not text:
                    return
                if result.is_final:
                    logger.info(f"[Deepgram] is_final: '{text[:80]}'")
                    conn._dg_final_buffer.append(text)
                else:
                    logger.info(f"[Deepgram] interim: '{text[:80]}'")
                # is_final = finalized segment → trigger EOU bounce.
                # EOU bounce handles multi-final dedup — only commits
                # after bounce delay with no new voice detected.
                if result.is_final:
                    conn._dg_final_buffer.append(text)
                    full_text = " ".join(conn._dg_final_buffer).strip()
                    conn.deepgram_transcript = full_text
                    logger.info(f"[Deepgram] is_final → EOU bounce: '{full_text[:120]}'")
                    if full_text and hasattr(conn, "_loop") and conn._loop and conn._loop.is_running():
                        asyncio.run_coroutine_threadsafe(
                            conn._on_stt_final(full_text), conn._loop
                        )
            except Exception:
                logger.exception("[Deepgram] on_message error")

        def on_error(error):
            logger.error(f"[Deepgram] STT WS error: {error}")

        # Enter sync context manager (connects WS) + register events
        conn._dg_ctx = client.listen.v1.connect(
            model=self._model, language=self._language,
            encoding="linear16", sample_rate=self._sample_rate,
            channels=1, smart_format=True, interim_results=True,
            punctuate=True, endpointing=350, utterance_end_ms=2000,
        )
        conn._dg_socket = conn._dg_ctx.__enter__()
        conn._dg_socket.on(EventType.MESSAGE, on_message)
        conn._dg_socket.on(EventType.ERROR, on_error)

        # start_listening() blocks on recv — run in daemon thread
        threading.Thread(target=conn._dg_socket.start_listening, daemon=True).start()
        logger.info("[Deepgram] Streaming STT started (sync client in thread)")

    async def close_audio_channels(self) -> None:
        logger.debug("[Deepgram] Closing STT WS")

    async def receive_audio(self, conn: Any, pcm: bytes, have_voice: bool) -> None:
        if not hasattr(conn, "_dg_socket"):
            return
        try:
            # send_media is sync (safe from asyncio — quick send)
            conn._dg_socket.send_media(pcm)
        except Exception as e:
            logger.warning(f"[Deepgram] send_media: {e}")

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
