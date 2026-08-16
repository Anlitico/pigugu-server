"""Cartesia TTS provider — text → Opus-encoded audio frames."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator
from typing import Any

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
        self._cartesia_client: Any | None = None

    async def _ensure_http(self) -> aiohttp.ClientSession:
        if self._http is None:
            timeout = aiohttp.ClientTimeout(total=30, connect=10)
            self._http = aiohttp.ClientSession(timeout=timeout)
        return self._http

    async def synthesize(
        self, text: str, raw_pcm: bool = False, collect_pcm: bytearray | None = None
    ) -> list[bytes]:
        """Convert text to a list of Opus-encoded (or raw PCM) binary frames.

        Each frame is one 60 ms chunk at the configured sample rate.
        Set ``raw_pcm=True`` to skip Opus encoding (for browser testing).
        If ``collect_pcm`` is provided, the raw PCM (before Opus encoding) is
        appended to it — useful for saving debug WAV files.
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

        if collect_pcm is not None:
            collect_pcm.extend(pcm)

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

    async def stream_audio(
        self,
        text_source: AsyncIterator[str],
        interrupt_event: asyncio.Event,
        collect_pcm: bytearray | None = None,
        receive_timeout: float = 15.0,
    ) -> AsyncIterator[list[bytes]]:
        """Stream LLM text through the Cartesia WebSocket TTS.

        Yields lists of Opus-encoded 60 ms frames as audio arrives —
        the caller paces them out with the playback clock. Cancelling the
        iterator aborts the context (barge-in). Raises ``RuntimeError`` on
        connection failures — the caller falls back to REST synthesis.

        Cartesia specifics (docs.cartesia.ai):
          - inputs are concatenated verbatim, so text chunks must carry
            their own spacing (LLM chunks already do);
          - ``continue_=True`` keeps prosody across chunks;
          - contexts expire ~1 s after the last audio — one per turn;
          - ``no_more_inputs()`` ends the stream; the receive loop then
            terminates on the ``done`` event.
        """
        if not self._api_key:
            logger.error("[Cartesia] CARTESIA_API_KEY not set")
            raise RuntimeError("Cartesia API key not set")

        try:
            from cartesia import AsyncCartesia  # pyright: ignore[reportMissingImports]
        except ImportError:
            logger.error("[Cartesia] SDK not installed — cannot stream")
            raise RuntimeError("cartesia SDK not installed")

        output_format = {
            "container": "raw",
            "encoding": "pcm_s16le",
            "sample_rate": self._sample_rate,
        }
        voice = {"mode": "id", "id": self._voice_id}

        # Reuse one SDK client per provider — creating a fresh one per turn
        # leaks httpx connection pools (the client is never closed per turn).
        if self._cartesia_client is None:
            self._cartesia_client = AsyncCartesia(api_key=self._api_key)
        client = self._cartesia_client
        encoder = _StreamOpusEncoder(self._sample_rate, channels=1, frame_duration_ms=60)

        try:
            manager = client.tts.websocket_connect()
            async with manager as ws:
                # timeout guards against a silent server (hang detection)
                ctx = ws.context(
                    model_id=self._model_id,
                    voice=voice,
                    output_format=output_format,
                    language="en",
                    timeout=receive_timeout,
                )

                async def _feed() -> None:
                    """Consume LLM text → Cartesia context (continue_=True).

                    Uses ctx.push() — unlike ctx.send(), it injects the
                    context defaults (voice/model/output_format). send()
                    requires `voice` as an explicit keyword argument.
                    """
                    try:
                        async for text in text_source:
                            if interrupt_event.is_set():
                                break
                            if not text:
                                continue
                            await ctx.push(text, continue_=True)
                    except Exception:
                        logger.exception("[Cartesia] feed failed")
                    finally:
                        try:
                            await ctx.no_more_inputs()
                        except Exception:
                            logger.warning("[Cartesia] no_more_inputs failed")

                feed_task = asyncio.ensure_future(_feed())
                received_audio = False

                # ── Chunk cadence diagnostics (locate the stutter source) ──
                last_chunk_at: float | None = None
                chunk_count = 0
                chunk_bytes = 0

                def _note_chunk(size: int) -> None:
                    nonlocal last_chunk_at, chunk_count, chunk_bytes
                    now = time.monotonic()
                    if last_chunk_at is not None:
                        gap = now - last_chunk_at
                        if gap > 0.5:
                            logger.warning(f"[Cartesia] chunk gap {gap:.2f}s (chunk #{chunk_count})")
                    last_chunk_at = now
                    chunk_count += 1
                    chunk_bytes += size
                    if chunk_count % 20 == 0:
                        avg = chunk_bytes / chunk_count
                        logger.info(
                            f"[Cartesia] chunk stats: {chunk_count} chunks, "
                            f"avg {avg:.0f} bytes ({avg/32000*1000:.0f}ms audio)"
                        )

                try:
                    async for event in ctx.receive():
                        if interrupt_event.is_set():
                            break
                        if event.type == "error":
                            raise RuntimeError(
                                f"[Cartesia] stream error: {getattr(event, 'error', 'unknown')}"
                            )
                        if event.type != "chunk" or not getattr(event, "audio", None):
                            continue
                        received_audio = True
                        _note_chunk(len(event.audio))
                        if collect_pcm is not None:
                            collect_pcm.extend(event.audio)
                        frames = encoder.feed(event.audio)
                        if frames:
                            yield frames
                except asyncio.CancelledError:
                    # barge-in: cancel generation, then propagate
                    await ctx.cancel()
                    raise
                except TimeoutError:
                    # A slow LLM can leave the audio queue idle longer than the
                    # receive timeout before the first chunk arrives. With no
                    # audio yet, surface the failure so the caller falls back
                    # to REST (the LLM text is still flowing and will be
                    # picked up there); with audio already played, end the
                    # turn gracefully.
                    if not received_audio:
                        raise RuntimeError("no audio before receive timeout") from None
                    logger.warning("[Cartesia] receive timeout — ending stream")
                finally:
                    if not feed_task.done():
                        feed_task.cancel()
                        try:
                            await feed_task
                        except (asyncio.CancelledError, Exception):
                            pass

            # flush any trailing partial frame at stream end
            tail = encoder.flush()
            if tail:
                yield tail
        except RuntimeError:
            raise
        except Exception as e:
            logger.exception("[Cartesia] streaming failed")
            raise RuntimeError(f"Cartesia streaming failed: {e}") from e

    async def close(self) -> None:
        if self._http:
            await self._http.close()
            self._http = None
        if self._cartesia_client is not None:
            await self._cartesia_client.close()
            self._cartesia_client = None


# ── Opus encoding helpers ─────────────────────────────────────────────

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


class _StreamOpusEncoder:
    """Incremental PCM → Opus encoder with a carry buffer.

    Cartesia WS chunks arrive at arbitrary sizes; playback needs fixed
    60 ms frames. ``feed()`` returns all complete frames produced by the
    new data, keeping the remainder for the next call. ``flush()`` emits
    the final partial frame (padded) so no tail audio is dropped.
    """

    def __init__(self, sample_rate: int, channels: int = 1, frame_duration_ms: int = 60):
        try:
            import opuslib  # pyright: ignore[reportMissingImports]

            self._encoder = opuslib.Encoder(sample_rate, channels, "voip")
        except ImportError:
            self._encoder = None
        self._frame_samples = frame_duration_ms * sample_rate // 1000
        self._frame_bytes = self._frame_samples * channels * 2
        self._carry = bytearray()

    def feed(self, pcm: bytes) -> list[bytes]:
        if not pcm:
            return []
        self._carry.extend(pcm)
        frames: list[bytes] = []
        while len(self._carry) >= self._frame_bytes:
            chunk = bytes(self._carry[: self._frame_bytes])
            del self._carry[: self._frame_bytes]
            if self._encoder is not None:
                try:
                    frames.append(self._encoder.encode(chunk, self._frame_samples))
                except Exception:
                    logger.warning("[Cartesia] opus encode failed for one frame")
            else:
                frames.append(chunk)  # raw PCM fallback (no opuslib)
        return frames

    def flush(self) -> list[bytes]:
        """Encode the final partial frame, zero-padded. Clears the carry."""
        if not self._carry:
            return []
        padded = bytes(self._carry) + b"\x00" * (self._frame_bytes - len(self._carry))
        self._carry.clear()
        if self._encoder is not None:
            try:
                return [self._encoder.encode(padded, self._frame_samples)]
            except Exception:
                return []
        return [padded]
