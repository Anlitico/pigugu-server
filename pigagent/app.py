"""App factory module — exposes `app` at module level for uvicorn reload."""
from api.server import create_app
from voice.server import router as xiaozhi_router

app = create_app()
app.include_router(xiaozhi_router)
