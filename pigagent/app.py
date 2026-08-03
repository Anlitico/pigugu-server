"""App factory — FastAPI REST API only (WebSocket moved to websockets server)."""
from api.server import create_app

app = create_app()
