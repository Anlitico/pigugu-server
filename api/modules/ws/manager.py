from fastapi import WebSocket


class WebSocketManager:
    def __init__(self) -> None:
        self._connections: dict[str, WebSocket] = {}

    async def connect(self, device_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections[device_id] = websocket

    def disconnect(self, device_id: str) -> None:
        self._connections.pop(device_id, None)

    async def broadcast(self, device_id: str, message: str) -> None:
        ws = self._connections.get(device_id)
        if ws:
            await ws.send_text(message)

    async def handle_message(self, device_id: str, data: str) -> None:
        ...


ws_manager = WebSocketManager()
