# pigagent/api/server.py
"""FastAPI application."""

from fastapi import FastAPI

from api.roast import router as roast_router


def create_app() -> FastAPI:
    app = FastAPI(title="Pigugu Agent", version="0.1.0")
    app.include_router(roast_router)
    return app
