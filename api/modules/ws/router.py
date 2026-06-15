"""Unified WebSocket endpoint for the App.

The App connects once at startup with its user_id + app_device_id.
All real-time events (provisioning, roast settlement, etc.) flow through
this single connection.
"""

import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query

from core.deps import get_current_user_ws
from modules.ws.manager import ws_manager

router = APIRouter(tags=["websocket"])


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
        (e.g. iOS UUID, Android ANDROID_ID)
    """
    user = await get_current_user_ws(token)
    if user is None:
        await websocket.accept()
        await websocket.send_json({"type": "error", "code": "AUTH_FAILED"})
        await websocket.close(code=4001)
        return

    user_id = str(user.id)
    await ws_manager.connect(user_id, app_device_id, websocket)
    key = ws_manager.make_key(user_id, app_device_id)

    try:
        while True:
            data = await websocket.receive_text()
            await ws_manager.handle_message(key, data)
    except WebSocketDisconnect:
        ws_manager.disconnect(user_id, app_device_id)
