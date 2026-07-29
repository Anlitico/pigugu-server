"""Proxy endpoints that forward authenticated requests to pigugu-agent.

The agent is an internal service (ClusterIP, no public exposure).
These endpoints validate user auth via JWT, then forward to the agent
with a trusted internal identifier.
Xiaozhi WebSocket connections are proxied at Layer 4 (raw TCP) for
bidirectional audio streaming.
"""

import asyncio
import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect

from core.deps import get_current_user
from models.user import User

AGENT_BASE = "http://pigugu-agent:8080"
AGENT_WS_BASE = "ws://pigugu-agent:8080"

logger = logging.getLogger(__name__)

router = APIRouter(tags=["agent"])


# ── WebSocket proxy: /v1/agent ────────────────────────────────────────────────

@router.websocket("/v1/agent")
async def xiaozhi_ws_proxy(websocket: WebSocket):
    """Proxy xiaozhi WebSocket connections to pigagent.

    Firmware connects to wss://api.pigugu.net/v1/agent.
    This endpoint accepts the WS connection and opens a corresponding
    connection to pigugu-agent:8080/v1/agent, then relays frames bidirectionally.
    """
    import websockets

    await websocket.accept()

    try:
        async with websockets.connect(f"{AGENT_WS_BASE}/v1/agent") as agent_ws:
            async def fw_to_agent():
                while True:
                    try:
                        data = await websocket.receive()
                        if "text" in data:
                            await agent_ws.send(data["text"])
                        elif "bytes" in data:
                            await agent_ws.send(data["bytes"])
                    except (WebSocketDisconnect, Exception):
                        break

            async def agent_to_fw():
                while True:
                    try:
                        msg = await agent_ws.recv()
                        if isinstance(msg, str):
                            await websocket.send_text(msg)
                        else:
                            await websocket.send_bytes(msg)
                    except Exception:
                        break

            await asyncio.gather(fw_to_agent(), agent_to_fw())
    except Exception as e:
        logger.error(f"[ws-proxy] agent connection failed: {e}")
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


# ── HTTP proxy: /livekit/token ────────────────────────────────────────────────


@router.get("/livekit/token")
async def livekit_token_proxy(
    room_name: str = Query(default="roast-room"),
    current_user: User = Depends(get_current_user),
):
    """Proxy LiveKit token requests to the agent with authenticated user_id."""
    user_id = str(current_user.id)

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(
                f"{AGENT_BASE}/livekit/token",
                params={"user_id": user_id, "room_name": room_name},
                timeout=10.0,
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            logger.error(f"[agent-proxy] livekit/token failed: {e}")
            raise HTTPException(status_code=502, detail="Agent unavailable")


