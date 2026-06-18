import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from sqlalchemy import delete

from core.database import AsyncSessionLocal
from core.redis import close_redis
from models.device_provisioning_session import DeviceProvisioningSession
from modules.auth.router import router as auth_router
from modules.device.iot import router as device_iot_router
from modules.device.router import router as device_router
from modules.game.router import leaderboard_router
from modules.game.router import router as game_router
from modules.gameplay.router import router as gameplay_router
from modules.news.router import router as news_router
from modules.push.router import router as push_router
from modules.push.service import init_firebase
from modules.agent.router import router as agent_router
from modules.ws.router import router as ws_router


async def _session_cleanup() -> None:
    """Periodically delete expired provisioning sessions older than 7 days."""
    logger = logging.getLogger(__name__)
    while True:
        await asyncio.sleep(3600)  # every hour
        try:
            async with AsyncSessionLocal() as db:
                cutoff = datetime.now(timezone.utc) - timedelta(days=7)
                result = await db.execute(
                    delete(DeviceProvisioningSession).where(
                        DeviceProvisioningSession.expires_at < cutoff
                    )
                )
                await db.commit()
                count = result.rowcount  # type: ignore[reportAttributeAccessIssue]
                if count:
                    logger.info("Cleaned up %d expired provisioning sessions", count)
        except Exception as e:
            logger.error("Session cleanup failed: %s", e)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_firebase()
    cleanup_task = asyncio.create_task(_session_cleanup())
    yield
    cleanup_task.cancel()
    await close_redis()


app = FastAPI(title="Pigugu Server", version="0.1.0", lifespan=lifespan)

app.include_router(auth_router, prefix="/v1")
app.include_router(device_router, prefix="/v1")
app.include_router(device_iot_router, prefix="/v1")
app.include_router(news_router, prefix="/v1")
app.include_router(game_router, prefix="/v1")
app.include_router(gameplay_router, prefix="/v1")
app.include_router(leaderboard_router, prefix="/v1")
app.include_router(push_router, prefix="/v1")
app.include_router(ws_router)
app.include_router(agent_router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
