"""WebSocket endpoint for real-time roast conversation streaming."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from loguru import logger

from bootstrap.factory import get_pig_agent, get_redis
from roast.event_bus import event_bus
from roast.session_registry import registry

router = APIRouter(prefix="/roast", tags=["roast"])


# ── Auth ────────────────────────────────────────────────────────────────────────


async def _validate_token(token: str) -> str | None:
    """Validate auth token and extract user_id.

    For v1, accepts a simple token format: "user_{user_id}".
    TODO: Implement proper JWT validation against pigugu-server auth service.
    """
    try:
        if token.startswith("user_"):
            return token[5:]
    except Exception:
        pass
    return None


# ── Active roast lookup ─────────────────────────────────────────────────────────


async def _lookup_active_roast(user_id: str, redis) -> dict | None:
    """Check if user has an active roast in Redis."""
    try:
        from roast.state import RoastState

        state = await RoastState._load_active(user_id, redis)
        if state and state.phase.value == "active":
            return {
                "roast_instance_id": state.roast_instance_id,
                "roast_id": state.roast_id,
                "mode": str(state.mode),
            }
    except Exception as e:
        logger.warning(f"[WS] Failed to lookup active roast for {user_id}: {e}")
    return None


# ── Main endpoint ───────────────────────────────────────────────────────────────


@router.websocket("/ws")
async def roast_websocket(
    websocket: WebSocket,
    token: str = Query(...),
):
    """WebSocket endpoint for roast conversation streaming.

    Steps:
    1. Validate auth token, extract user_id
    2. Accept WS connection
    3. Subscribe to event bus for this user_id
    4. Loop: receive client messages AND forward event bus events
    5. On disconnect: unsubscribe from event bus
    """
    # ── Auth ─────────────────────────────────────────────────
    user_id = await _validate_token(token)
    if not user_id:
        await websocket.accept()
        await websocket.send_json({
            "type": "error",
            "code": "AUTH_FAILED",
            "message": "Invalid or expired token",
        })
        await websocket.close(code=4001)
        return

    await websocket.accept()
    logger.info(f"[WS] Connected: user_id={user_id}")

    redis = get_redis()
    queue = await event_bus.subscribe(user_id)
    pig_agent = await get_pig_agent()

    # Check for existing active roast
    active_roast = await _lookup_active_roast(user_id, redis)
    await websocket.send_json({
        "type": "connected",
        "user_id": user_id,
        "active_roast": active_roast,
    })

    # ── Background task: forward event bus events to WS ──────
    async def _event_forwarder():
        """Read from event bus queue and send to WebSocket."""
        while True:
            try:
                event = await queue.get()
                await websocket.send_json(event)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning(f"[WS] Forward error for {user_id}: {exc}")
                break

    forwarder_task = asyncio.create_task(_event_forwarder())

    try:
        # ── Main loop: handle client messages ─────────────────
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            msg_type = msg.get("type", "")

            if msg_type == "start_roast":
                await _handle_start_roast(websocket, pig_agent, user_id, msg)

            elif msg_type == "subscribe":
                riid = msg.get("roast_instance_id", "")
                if riid and active_roast and active_roast["roast_instance_id"] == riid:
                    await websocket.send_json({
                        "type": "roast_event",
                        "event": "resumed",
                        "roast_instance_id": riid,
                    })
                else:
                    await websocket.send_json({
                        "type": "error",
                        "code": "ROAST_NOT_FOUND",
                        "message": f"No active roast for roast_instance_id={riid}",
                    })

            elif msg_type == "ping":
                await websocket.send_json({"type": "pong"})

            else:
                await websocket.send_json({
                    "type": "error",
                    "code": "UNKNOWN_MESSAGE_TYPE",
                    "message": f"Unknown message type: {msg_type}",
                })

    except WebSocketDisconnect:
        logger.info(f"[WS] Disconnected: user_id={user_id}")
    except Exception as e:
        logger.error(f"[WS] Error for {user_id}: {e}")
    finally:
        forwarder_task.cancel()
        await event_bus.unsubscribe(user_id, queue)
        logger.info(f"[WS] Cleaned up: user_id={user_id}")


# ── Message handlers ────────────────────────────────────────────────────────────


async def _handle_start_roast(
    websocket: WebSocket,
    pig_agent: Any,
    user_id: str,
    msg: dict[str, Any],
) -> None:
    """Handle a start_roast message: activate and stream the opening reply.

    Routing logic:
    - If user has an active LiveKit session → inject into session TTS pipeline
      (opening lines play through room audio, all participants hear it).
    - If no active session → stream text via WebSocket (App handles display/TTS).
      TODO: MQTT wake hardware, wait for it to join room, then inject.
    """
    persona_id = msg.get("persona_id", 1)
    roast_id = msg.get("roast_id", "")
    mode_id = msg.get("mode_id", "")
    prompt = msg.get("prompt", "")
    headline = msg.get("headline", "")
    source = msg.get("source", "")

    # Validate required fields
    if not roast_id or not mode_id or not prompt:
        await websocket.send_json({
            "type": "error",
            "code": "INVALID_PARAMS",
            "message": "roast_id, mode_id, and prompt are required",
        })
        return

    session_active = await registry.has_active_agent(user_id)

    if session_active:
        # ── Route through LiveKit room data channel ───────────────
        logger.info(
            f"[WS] start_roast: routing to LiveKit room "
            f"user={user_id} roast={roast_id}"
        )
        await registry.send_inject(user_id, {
            "type": "start_roast",
            "persona_id": persona_id,
            "roast_id": roast_id,
            "mode_id": mode_id,
            "prompt": prompt,
            "headline": headline,
            "source": source,
        })
        await websocket.send_json({
            "type": "roast_event",
            "event": "roast_started_in_room",
            "roast_id": roast_id,
            "mode_id": mode_id,
        })
        # session.py receives this via LiveKit data_received,
        # runs pig_agent.start_roast() → session.say() → TTS broadcast

    else:
        # ── No active session — stream text via WS ───────────────
        # TODO: MQTT wake hardware → wait for join → then inject
        logger.info(
            f"[WS] start_roast: no LiveKit session, streaming via WS "
            f"user={user_id} roast={roast_id}"
        )
        try:
            async for text in pig_agent.start_roast(
                user_id=user_id,
                persona_id=persona_id,
                roast_id=roast_id,
                mode_id=mode_id,
                prompt=prompt,
                headline=headline,
                source=source,
            ):
                chunk = text if isinstance(text, str) else ""
                if chunk:
                    await websocket.send_json({
                        "type": "agent_response",
                        "text": chunk,
                        "final": False,
                    })

            # Signal end of opening turn
            await websocket.send_json({
                "type": "agent_response",
                "text": "",
                "final": True,
            })

            # No active room — flag this for the App
            await websocket.send_json({
                "type": "state_change",
                "state": "listening",
                "needs_room": True,
            })

            await event_bus.publish(user_id, {
                "type": "roast_event",
                "event": "roast_started",
                "roast_id": roast_id,
            })

        except Exception as exc:
            logger.error(f"[WS] start_roast failed for {user_id}: {exc}")
            await websocket.send_json({
                "type": "error",
                "code": "ROAST_START_FAILED",
                "message": str(exc),
            })
