"""Unified WebSocket endpoint for the App.

The App connects once at startup with its user_id + app_device_id.
All real-time events (provisioning, roast, settlement, etc.) flow through
this single connection.
"""

import json

import httpx
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query

from core.deps import get_current_user_ws
from modules.ws.manager import ws_manager

router = APIRouter(tags=["websocket"])

AGENT_BASE = "http://pigugu-agent:8080"


@router.websocket("/ws/app")
async def websocket_app(
    websocket: WebSocket,
    token: str = Query(...),
    app_device_id: str = Query(...),
):
    """App WebSocket — single connection for all real-time events.

    Query params:
      - token: JWT access token
      - app_device_id: unique device fingerprint from the App
    """
    user = await get_current_user_ws(token)
    if user is None:
        await websocket.accept()
        await websocket.send_json({"type": "error", "code": "AUTH_FAILED"})
        await websocket.close(code=4001)
        return

    user_id = str(user.id)
    await ws_manager.connect(user_id, app_device_id, websocket)
    # NOTE: ws_manager.connect() accepts the WS internally
    import logging
    logging.getLogger(__name__).info("WS message loop started for user=%s", user_id)

    try:
        while True:
            raw = await websocket.receive_text()
            await _handle_message(websocket, user, raw)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        import logging
        logging.getLogger(__name__).exception("WS handler crashed for %s", user_id)
        try:
            await websocket.send_json({"type": "error", "code": "INTERNAL", "message": str(e)})
        except Exception:
            pass
    finally:
        ws_manager.disconnect(user_id, app_device_id)


async def _handle_message(
    websocket: WebSocket,
    user,
    raw: str,
) -> None:
    """Route incoming WS messages to the appropriate handler."""
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        await websocket.send_json({
            "type": "error", "code": "INVALID_JSON",
            "message": "Message must be valid JSON",
        })
        return

    msg_type = msg.get("type", "")

    if msg_type == "start_roast":
        await _handle_start_roast(websocket, user, msg)
    elif msg_type == "ping":
        await websocket.send_json({"type": "pong"})
    else:
        await websocket.send_json({
            "type": "error", "code": "UNKNOWN_MESSAGE_TYPE",
            "message": f"Unknown message type: {msg_type}",
        })


async def _handle_start_roast(
    websocket: WebSocket,
    user,
    msg: dict,
) -> None:
    """Handle start_roast: forward to pigagent, then relay result to App.

    Uses UUID as the room identifier — canonical user_id across all
    three ends (app, server, firmware).
    """
    roast_id = msg.get("roast_id", "")
    mode_id = msg.get("mode_id", "")
    prompt = msg.get("prompt", "")

    if not roast_id or not mode_id or not prompt:
        await websocket.send_json({
            "type": "error", "code": "INVALID_PARAMS",
            "message": "roast_id, mode_id, and prompt are required",
        })
        return

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(
                f"{AGENT_BASE}/roast/start-sync",
                json={
                    # Use email as user_id → room_name = email in LiveKit
                    "user_id": str(user.id),
                    "persona_id": msg.get("persona_id", 1),
                    "roast_id": roast_id,
                    "mode_id": mode_id,
                    "prompt": prompt,
                    "headline": msg.get("headline", ""),
                    "source": msg.get("source", ""),
                },
                timeout=30.0,
            )
            resp.raise_for_status()
            result = resp.json()
        except httpx.HTTPError as e:
            await websocket.send_json({
                "type": "error", "code": "AGENT_UNAVAILABLE",
                "message": str(e),
            })
            return

    if result.get("settled_in_room"):
        # Agent is in room — TTS will play there
        await websocket.send_json({
            "type": "roast_event",
            "event": "roast_started_in_room",
            "roast_id": roast_id,
            "mode_id": mode_id,
        })
    elif result.get("text"):
        # No agent — stream text back to App
        text = result["text"]
        # Send as a single final agent_response (the pigagent already collected it)
        await websocket.send_json({
            "type": "agent_response",
            "text": text,
            "final": True,
        })
        await websocket.send_json({
            "type": "state_change",
            "state": "listening",
            "needs_room": True,
        })
