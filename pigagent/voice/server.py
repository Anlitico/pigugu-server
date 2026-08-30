"""WebSocket server — runs the Pipecat voice pipeline per device connection.

One ``PiguguSession`` per device WebSocket (the firmware protocol v1: raw
Opus + JSON control messages). The session builds the pipeline (VAD/STT
bridges → turn processor → storage observer → agent gateway → TTS bridge)
and runs it until the connection ends. The PigAgent is created lazily by the
TTS bridge on the first turn (needs the user id + hw_id from the hello).

The connection registry is shared with the REST API for roast inject.
"""

from __future__ import annotations

import asyncio
import logging
import os

import websockets
from loguru import logger

from voice.pipecat.session import PiguguSession

# The NLB TCP health check connects to :8080 and immediately closes; websockets
# (asyncio server, 17.x) logs INFO "connection closed" for every such probe
# (~3.7/s), flooding the log. Keep WARNING+ (real failures like handler errors)
# visible; connection lifecycle is logged explicitly below.
logging.getLogger("websockets.server").setLevel(logging.WARNING)

# ── Connection registry (shared with REST API for roast inject) ──────

_connections: dict[str, PiguguSession] = {}


async def has_active_connection(user_id: str) -> bool:
    return user_id in _connections


async def send_inject(user_id: str, msg: dict) -> None:
    session = _connections.get(user_id)
    if session is None:
        logger.warning(f"[Voice] No active connection for user={user_id}")
        return
    await session.inject(msg)


# ── Shared provider singletons ───────────────────────────────────────

from providers.base import VADProvider, STTProvider, TTSProvider  # noqa: E402

_shared_vad: VADProvider | None = None
_shared_stt: STTProvider | None = None
_shared_tts: TTSProvider | None = None


def _get_shared_vad():
    global _shared_vad
    if _shared_vad is None:
        from providers.vad.onnx import SileroVAD
        _shared_vad = SileroVAD(
            threshold=0.5,
            threshold_low=0.2,
            min_silence_duration_ms=700,
        )
    return _shared_vad


def _get_shared_stt():
    global _shared_stt
    if _shared_stt is None:
        from providers.stt.deepgram import DeepgramSTT
        _shared_stt = DeepgramSTT()
    return _shared_stt


def _get_shared_tts():
    global _shared_tts
    if _shared_tts is None:
        from providers.tts.cartesia import CartesiaTTS
        _shared_tts = CartesiaTTS()
    return _shared_tts


# ── WebSocket handler ────────────────────────────────────────────────


async def _on_connect(ws: websockets.ServerConnection) -> None:
    """Handle one device connection — official pattern."""
    # Extract client-id from path or headers (xiaozhi protocol)
    path = ws.request.path if hasattr(ws, 'request') else "/"
    client_id = ""
    device_id = ""

    for header_name in ("client-id", "Client-Id", "device-id", "Device-Id"):
        if header_name in (ws.request.headers if hasattr(ws, 'request') else {}):
            val = ws.request.headers[header_name]
            if header_name.lower().startswith("client"):
                client_id = val
            else:
                device_id = val

    if not client_id and device_id:
        client_id = device_id

    protocol_ver = ""
    if hasattr(ws, 'request'):
        protocol_ver = ws.request.headers.get("protocol-version", ws.request.headers.get("Protocol-Version", ""))

    logger.info(
        f"[Voice] New connection client_id={client_id} "
        f"device_id={device_id} protocol={protocol_ver}"
    )

    # The PigAgent is lazy (created by the TTS bridge on the first turn, once
    # the device hello provides user/hw identity). Providers are shared.
    session = PiguguSession(
        ws,
        client_id=client_id,
        user_id=client_id,
        vad=_get_shared_vad(),
        stt=_get_shared_stt(),
        tts=_get_shared_tts(),
    )
    _connections[client_id] = session
    try:
        await session.run()
    finally:
        _connections.pop(client_id, None)
        logger.info(f"[Voice] Session ended client_id={client_id}")


# ── Main entry ────────────────────────────────────────────────────────

async def run_server(host: str = "0.0.0.0", port: int = 8080) -> None:
    """Start the WebSocket server (official pattern)."""
    logger.info(f"[Voice] Starting websocket server on {host}:{port}")
    # Pre-load providers at startup so the first connection doesn't pay
    # the cold-start cost (ONNX VAD model load ~1 s).
    _get_shared_vad()
    _get_shared_stt()
    _get_shared_tts()
    logger.info("[Voice] All providers initialized")
    async with websockets.serve(_on_connect, host, port):
        await asyncio.Future()  # run forever


def start_server(host: str = "0.0.0.0", port: int = 8080) -> None:
    """Blocking entry point (called from main.py)."""
    asyncio.run(run_server(host, port))
