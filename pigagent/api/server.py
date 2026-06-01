# pigagent/api/server.py
"""FastAPI application  -  runs alongside LiveKit in the same process."""

from fastapi import FastAPI

from api.roast import router as roast_router
from api.roast_ws import router as roast_ws_router
from api.livekit_token import router as livekit_token_router


def create_app() -> FastAPI:
    app = FastAPI(title="Pigugu Agent", version="0.1.0")
    app.include_router(roast_router)
    app.include_router(roast_ws_router)
    app.include_router(livekit_token_router)
    return app
