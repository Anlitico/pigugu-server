"""AssemblyAI Universal-3.5 Pro Realtime STT provider — WebSocket streaming.

Mirrors the Deepgram provider's callback contract so the same
``PiguguSttBridge`` consumes it without changes:

  - ``open_audio_channels(conn)`` opens one streaming WS for the session and
    sets ``conn._stt_open`` (the generic idempotency flag read by the bridge).
  - ``receive_audio(conn, pcm, ...)`` pushes raw PCM frames as binary frames.
  - Results are dispatched to the bridge via ``conn._on_stt_interim`` /
    ``conn._on_stt_final`` / ``conn._on_utterance_end``.

Turn-end is driven by the model's semantic (punctuation-based) endpointing:
a ``Turn`` message with ``end_of_turn=true`` fires ``_on_utterance_end``, which
the bridge maps to a ``ProposedUserStoppedSpeakingFrame`` (``turn_end_signal``
= "external"). This is what makes mid-sentence pauses survive — verified in
PoC: a 700ms mid-sentence pause does not split a turn, while two real turns
split cleanly.

Protocol (v3, verified against pipecat 1.8.1 reference):
  - endpoint  wss://streaming.assemblyai.com/v3/ws
  - auth      raw API key in the Authorization header (NO "Bearer " prefix)
  - audio     raw binary WebSocket frames (pcm_s16le, 16kHz mono)
  - events    Turn (transcript / end_of_turn), SpeechStarted, Termination
  - billing   by open-connection time — close promptly on session end.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

import aiohttp
from loguru import logger

from providers.base import InterfaceType, STTProvider

_WS_URL = "wss://streaming.assemblyai.com/v3/ws"
# Floor between reconnect attempts for a failing endpoint. A handshake that is
# accepted then immediately closed (invalid params, server-side error) would
# otherwise reconnect on every audio frame (~50/s), each a billed connection.
_RECONNECT_BACKOFF_S = 5.0


class AssemblyAISttProvider(STTProvider):
    """AssemblyAI U3.5 Pro Realtime — streaming via v3 WebSocket."""

    interface_type = InterfaceType.STREAM
    # Turn-end is driven by the model's semantic endpointing (end_of_turn),
    # not by our VAD — the bridge emits a ProposedUserStoppedSpeakingFrame.
    turn_end_signal = "external"
    # The decoder consumes conversation context (the agent's last spoken reply)
    # via the framework: the TTS bridge pushes each completed reply through the
    # STT bridge → update_context → UpdateConfiguration(agent_context).
    supports_context = True
    max_context_chars = 1750

    def __init__(
        self,
        api_key: str = "",
        model: str = "u3-rt-pro",
        language: str = "en",
        sample_rate: int = 16000,
        min_turn_silence: int = 300,
        max_turn_silence: int = 1500,
        keyterms: list[str] | None = None,
    ):
        self._api_key = api_key or os.getenv("ASSEMBLYAI_API_KEY", "")
        self._model = model
        self._language = language
        self._sample_rate = sample_rate
        self._min_turn_silence = min_turn_silence
        self._max_turn_silence = max_turn_silence
        self._keyterms = [k for k in (keyterms or []) if k.strip()]

    # ── Streaming ─────────────────────────────────────────────────────

    async def open_audio_channels(self, conn: Any) -> None:
        """Open one AssemblyAI streaming WS for the session (idempotent)."""
        if getattr(conn, "_stt_open", False):
            return
        # Bound reconnect attempts for a failing endpoint: a handshake that is
        # accepted then immediately closed would otherwise reconnect on every
        # audio frame (~50/s), each a billed connection.
        if time.monotonic() - getattr(conn, "_aai_open_attempt_at", 0.0) < _RECONNECT_BACKOFF_S:
            return
        conn._aai_open_attempt_at = time.monotonic()
        if not self._api_key:
            # Mark attempted so the per-frame open guard stops here — an
            # unconfigured key must not log at frame rate.
            conn._stt_open = True
            logger.error("[AssemblyAI] ASSEMBLYAI_API_KEY not set")
            return

        params = {
            "sample_rate": self._sample_rate,
            "encoding": "pcm_s16le",
            "speech_model": self._model,
            "min_turn_silence": self._min_turn_silence,
            "max_turn_silence": self._max_turn_silence,
        }
        if self._keyterms:
            # keyterms_prompt takes ONE JSON-array string (repeated query keys
            # are rejected: server error 3006 "Invalid JSON array").
            params["keyterms_prompt"] = json.dumps(self._keyterms)
        # Seed the session with whatever context the framework already pushed
        # for this connection (e.g. the agent replied before the stream opened).
        conn_context = getattr(conn, "_aai_agent_context", "")
        if conn_context:
            params["agent_context"] = conn_context
        url = _build_url(_WS_URL, params)

        session = None
        try:
            timeout = aiohttp.ClientWSTimeout(ws_receive=120)
            session = aiohttp.ClientSession()
            ws = await session.ws_connect(
                url,
                headers={"Authorization": self._api_key},  # raw key, no Bearer
                timeout=timeout,
                receive_timeout=120,
            )
        except Exception as e:
            # Mark attempted so the bridge stops retrying every frame (a
            # per-frame reconnect storm would stall the audio pipeline), and
            # close the session we created — otherwise the TCP connector leaks.
            # Clear the stale socket refs so receive_audio/update_context don't
            # write to a dead ws and spam warnings.
            conn._stt_open = True
            conn._aai_ws = None
            conn._aai_http = None
            logger.error(f"[AssemblyAI] connect failed: {e!r}")
            if session is not None:
                try:
                    await session.close()
                except Exception:
                    pass
            return

        conn._stt_open = True
        conn._aai_http = session
        conn._aai_ws = ws
        conn._aai_task = asyncio.create_task(self._receive_loop(conn, ws, session))
        logger.info(f"[AssemblyAI] streaming open model={self._model}")

    async def _receive_loop(
        self, conn: Any, ws: aiohttp.ClientWebSocketResponse, session: aiohttp.ClientSession
    ) -> None:
        """Read server events and dispatch to the bridge on its loop."""
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    evt = msg.json()
                    self._dispatch(conn, evt)
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                    break
        except Exception:
            logger.exception("[AssemblyAI] receive loop ended")
        finally:
            conn._stt_open = False
            # Close the CAPTURED ws/session, not conn._aai_ws/_aai_http — those
            # may point at a freshly-opened session if a reconnect raced this
            # loop's exit, and closing them would kill the new session. Clear the
            # conn refs so a later send can't touch the dead socket.
            try:
                await ws.close()
            except Exception:
                pass
            try:
                await session.close()
            except Exception:
                pass
            # Only clear the refs if they still point at THIS ws/session — a
            # reconnect that completed while we were closing would otherwise be
            # clobbered.
            if conn._aai_ws is ws:
                conn._aai_ws = None
            if conn._aai_http is session:
                conn._aai_http = None

    def _dispatch(self, conn: Any, evt: dict) -> None:
        kind = evt.get("type") or evt.get("message_type")
        if kind == "Turn":
            text = evt.get("transcript", "") or ""
            if evt.get("end_of_turn"):
                self._schedule(conn, conn._on_stt_final(text))
                self._schedule(conn, conn._on_utterance_end())
            elif text:
                self._schedule(conn, conn._on_stt_interim(text))
        elif kind == "SpeechStarted":
            logger.debug("[AssemblyAI] SpeechStarted (turn start / barge-in)")
        elif kind in ("Termination", "SessionTerminated"):
            logger.info(
                f"[AssemblyAI] terminated audio={evt.get('audio_duration_seconds')}s "
                f"session={evt.get('session_duration_seconds')}s"
            )
        elif kind == "error":
            logger.error(f"[AssemblyAI] server error: {evt}")
        else:
            logger.debug(f"[AssemblyAI] event {kind}")

    @staticmethod
    def _schedule(conn: Any, coro) -> None:
        """Dispatch a coroutine on the pipeline loop (Deepgram does the same
        via run_coroutine_threadsafe; our receive loop runs as a task on the
        pipeline loop, so the dispatch is scheduled back onto it)."""
        loop = getattr(conn, "_loop", None)
        if loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(coro, loop)
        except Exception:
            logger.exception("[AssemblyAI] dispatch failed")

    async def receive_audio(self, conn: Any, pcm: bytes, have_voice: bool) -> None:
        if not getattr(conn, "_aai_ws", None):
            return
        try:
            await conn._aai_ws.send_bytes(pcm)
        except Exception as e:
            logger.warning(f"[AssemblyAI] send: {e}")

    async def update_context(self, conn: Any, context: str) -> None:
        """Carry the agent's last spoken reply into the decoder.

        Sent mid-stream via UpdateConfiguration (no reconnect) when the WS is
        open; always cached on the connection so a fresh stream can seed it in
        the URL. Context is per-connection — the provider is a shared singleton.
        """
        conn._aai_agent_context = context
        ws = getattr(conn, "_aai_ws", None)
        if ws is None:
            return
        try:
            await ws.send_str(
                json.dumps({"type": "UpdateConfiguration", "agent_context": context})
            )
        except Exception as e:
            logger.warning(f"[AssemblyAI] update_context send: {e}")

    async def close_audio_channels(self) -> None:
        pass  # per-connection close is done via close_connection(conn)

    async def close_connection(self, conn: Any) -> None:
        """Cancel the receive loop and close the WS — billing is by open time."""
        task = getattr(conn, "_aai_task", None)
        if task:
            task.cancel()
        ws = getattr(conn, "_aai_ws", None)
        if ws:
            try:
                await ws.send_str('{"type": "Terminate"}')
            except Exception:
                pass
            try:
                await ws.close()
            except Exception:
                pass
        http = getattr(conn, "_aai_http", None)
        if http:
            try:
                await http.close()
            except Exception:
                pass
        conn._stt_open = False

    # ── Batch fallback (unused for streaming providers) ───────────────

    async def transcribe(self, pcm: bytes) -> str:
        logger.debug("[AssemblyAI] batch transcribe not used for streaming")
        return ""


def _build_url(base: str, params: dict) -> str:
    from urllib.parse import urlencode

    return f"{base}?{urlencode(params)}"
