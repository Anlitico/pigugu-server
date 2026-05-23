from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from modules.ws.manager import ws_manager

router = APIRouter(tags=["websocket"])


@router.websocket("/ws/device/{device_id}")
async def websocket_device(websocket: WebSocket, device_id: str):
    await ws_manager.connect(device_id, websocket)
    try:
        while True:
            data = await websocket.receive_text()
            await ws_manager.handle_message(device_id, data)
    except WebSocketDisconnect:
        ws_manager.disconnect(device_id)
