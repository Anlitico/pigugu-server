# pigagent/ws/server.py
"""FastAPI WebSocket endpoint for xiaozhi protocol.

Mounts the xiaozhi WebSocket handler at /ws/v1/xiaozhi.
Firmware connects to wss://<host>/ws/v1/xiaozhi with:
  - Authorization: Bearer <token>
  - Protocol-Version: 1
  - Device-Id: <MAC>
  - Client-Id: <UUID>
"""

from __future__ import annotations

from fastapi import APIRouter, WebSocket
from loguru import logger

from ws.handler import XiaozhiHandler

router = APIRouter(prefix="/v1/agent", tags=["agent"])

# ── Global connection registry ────────────────────────────────────

_connections: dict[str, XiaozhiHandler] = {}


async def has_active_connection(user_id: str) -> bool:
    """Check if a WS connection is active for a given user."""
    return user_id in _connections


async def send_inject(user_id: str, msg: dict) -> None:
    """Send a roast_inject command to an active WS connection."""
    handler = _connections.get(user_id)
    if handler is None:
        raise ValueError(f"No active connection for user: {user_id}")
    await handler.inject_roast(msg)


@router.websocket("")
async def xiaozhi_websocket(ws: WebSocket) -> None:
    """Xiaozhi protocol WebSocket endpoint — wss://host/v1/agent"""
    client_id = ws.headers.get("client-id", "") or ws.headers.get("Client-Id", "")
    device_id = ws.headers.get("device-id", "") or ws.headers.get("Device-Id", "")
    protocol_version = ws.headers.get("protocol-version", "") or ws.headers.get("Protocol-Version", "")
    auth = ws.headers.get("authorization", "") or ws.headers.get("Authorization", "")

    logger.info(
        f"[Xiaozhi] New connection: "
        f"client_id={client_id} device_id={device_id} "
        f"protocol={protocol_version} auth={'present' if auth else 'missing'}"
    )

    handler = XiaozhiHandler(ws, client_id=client_id)
    _connections[client_id] = handler
    try:
        await handler.run()
    finally:
        _connections.pop(client_id, None)
