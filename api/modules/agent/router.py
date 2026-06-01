"""Proxy endpoints that forward authenticated requests to pigugu-agent.

The agent is an internal service (ClusterIP, no public exposure).
These endpoints validate user auth via JWT, then forward to the agent
with a trusted internal identifier.
"""

import asyncio
import logging

import httpx
import websockets
from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect

from core.deps import get_current_user
from models.user import User

AGENT_BASE = "http://pigugu-agent:8080"

logger = logging.getLogger(__name__)

router = APIRouter(tags=["agent"])


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


# ── WebSocket proxy: /roast/ws ─────────────────────────────────────────────────


@router.websocket("/roast/ws")
async def roast_ws_proxy(
    websocket: WebSocket,
    token: str = Query(...),
):
    """WebSocket proxy: validate JWT, then bridge to agent's /roast/ws.

    The client sends its JWT as a ?token= query param (since WS handshake
    has no Authorization header). We validate it, extract user_id, then
    connect to the agent internally using an internal user_<id> token.
    """
    # ── Validate JWT ───────────────────────────────────────────
    try:
        from core.database import AsyncSessionLocal
        from core.security import decode_access_token
        from modules.auth.service import get_user_by_id
        import uuid

        payload = decode_access_token(token)
        user_id_str: str = payload.get("sub")
        if not user_id_str:
            await websocket.accept()
            await websocket.send_json({
                "type": "error",
                "code": "AUTH_FAILED",
                "message": "Invalid token: missing subject",
            })
            await websocket.close(code=4001)
            return

        user_uuid = uuid.UUID(user_id_str)
        async with AsyncSessionLocal() as db:
            user = await get_user_by_id(db, user_uuid)
        if not user:
            await websocket.accept()
            await websocket.send_json({
                "type": "error",
                "code": "AUTH_FAILED",
                "message": "User not found",
            })
            await websocket.close(code=4001)
            return
    except (ValueError, Exception) as e:
        await websocket.accept()
        await websocket.send_json({
            "type": "error",
            "code": "AUTH_FAILED",
            "message": f"Invalid token: {e}",
        })
        await websocket.close(code=4001)
        return

    await websocket.accept()
    logger.info(f"[agent-proxy] WS connected: user_id={user_id_str}")

    # ── Connect to agent internally ────────────────────────────
    agent_ws_url = f"ws://pigugu-agent:8080/roast/ws?token=user_{user_id_str}"

    try:
        async with websockets.connect(agent_ws_url) as agent_ws:
            # ── Bidirectional forwarder ────────────────────────
            async def client_to_agent():
                """Forward messages from app → agent."""
                while True:
                    try:
                        data = await websocket.receive_text()
                        await agent_ws.send(data)
                    except WebSocketDisconnect:
                        break
                    except Exception:
                        break

            async def agent_to_client():
                """Forward messages from agent → app."""
                while True:
                    try:
                        data = await agent_ws.recv()
                        await websocket.send_text(data)
                    except websockets.exceptions.ConnectionClosed:
                        break
                    except Exception:
                        break

            # Run both directions concurrently
            done, pending = await asyncio.wait(
                [
                    asyncio.create_task(client_to_agent()),
                    asyncio.create_task(agent_to_client()),
                ],
                return_when=asyncio.FIRST_COMPLETED,
            )

            # Cancel the other direction
            for task in pending:
                task.cancel()

    except Exception as e:
        logger.error(f"[agent-proxy] Agent WS connection failed: {e}")
        await websocket.send_json({
            "type": "error",
            "code": "AGENT_UNAVAILABLE",
            "message": "Roast agent is not available",
        })
    finally:
        logger.info(f"[agent-proxy] WS disconnected: user_id={user_id_str}")
