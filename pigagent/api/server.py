# pigagent/api/server.py
"""FastAPI application."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from api.roast import router as roast_router

_TEST_DIR = Path(__file__).resolve().parent.parent / "tests" / "manual"


def create_app() -> FastAPI:
    app = FastAPI(title="Pigugu Agent", version="0.1.0")
    app.include_router(roast_router)

    # Serve manual test pages at /test/
    if _TEST_DIR.is_dir():
        app.mount("/test", StaticFiles(directory=str(_TEST_DIR), html=True), name="test")

    return app
