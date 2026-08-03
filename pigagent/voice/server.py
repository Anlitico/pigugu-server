"""WebSocket server — FastAPI endpoint + connection registry.

Replaces the old ``ws/server.py``.
"""

from __future__ import annotations

from fastapi import APIRouter, WebSocket
from loguru import logger

from voice.connection import ConnectionHandler

router = APIRouter(prefix="/v1/agent", tags=["agent"])

# ── Connection registry (shared with REST API for roast inject) ──────

_connections: dict[str, ConnectionHandler] = {}


async def has_active_connection(user_id: str) -> bool:
    """Check if a device has an active WebSocket connection."""
    return user_id in _connections


async def send_inject(user_id: str, msg: dict) -> None:
    """Push a roast / control message into an active connection."""
    handler = _connections.get(user_id)
    if handler is None:
        logger.warning(f"[Voice] No active connection for user={user_id}")
        return
    await handler.inject_roast(msg)


# ── WebSocket endpoint ───────────────────────────────────────────────


@router.websocket("")
async def xiaozhi_websocket(ws: WebSocket) -> None:
    """Handle a single xiaozhi-protocol WebSocket connection."""
    client_id = (
        ws.headers.get("client-id")
        or ws.headers.get("Client-Id")
        or ws.headers.get("device-id")
        or ws.headers.get("Device-Id")
        or ""
    )
    device_id = ws.headers.get("device-id") or ws.headers.get("Device-Id") or ""
    protocol_ver = ws.headers.get("protocol-version") or ws.headers.get(
        "Protocol-Version"
    )
    auth = ws.headers.get("authorization") or ws.headers.get("Authorization") or "missing"

    logger.info(
        f"[Voice] New connection client_id={client_id} "
        f"device_id={device_id} protocol={protocol_ver} auth={auth}"
    )

    # ── Create handler with shared providers (lazy-init from factory) ─
    from providers.stt.deepgram import DeepgramSTT
    from providers.tts.cartesia import CartesiaTTS

    handler = ConnectionHandler(ws, client_id=client_id)

    # ---- Shared providers (singleton-like, created once at module level) ----
    handler.vad = _get_shared_vad()
    handler.stt = _get_shared_stt()
    handler.tts = _get_shared_tts()

    _connections[client_id] = handler
    try:
        await handler.run()
    finally:
        _connections.pop(client_id, None)


# ── Shared provider singletons (lazy-init) ───────────────────────────

from providers.base import VADProvider, STTProvider, TTSProvider  # noqa: E402

_shared_vad: VADProvider | None = None
_shared_stt: STTProvider | None = None
_shared_tts: TTSProvider | None = None


def _get_shared_vad():
    global _shared_vad
    if _shared_vad is None:
        from providers.vad.onnx import SileroVAD

        _shared_vad = SileroVAD(
            threshold=0.1,
            threshold_low=0.05,
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
