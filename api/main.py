import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from sqlalchemy import delete

from core.database import AsyncSessionLocal
from core.redis import close_redis, get_redis
from models.device_provisioning_session import DeviceProvisioningSession
from modules.auth.router import router as auth_router
from modules.device.iot import router as device_iot_router
from modules.device.router import router as device_router
from modules.game.router import leaderboard_router
from modules.game.router import router as game_router
from modules.game.service import handle_roast_settled
from modules.gameplay.router import router as gameplay_router
from modules.news.router import router as news_router
from modules.push.router import router as push_router
from modules.push.service import init_firebase
from modules.agent.router import router as agent_router
from modules.ws.router import router as ws_router


async def _redis_subscriber() -> None:
    redis = await get_redis()
    pubsub = redis.pubsub()
    await pubsub.psubscribe("roast:settled:*")
    async for message in pubsub.listen():
        if message["type"] != "pmessage":
            continue
        channel: str = message["channel"]
        data = message["data"]

        if channel.startswith("roast:settled:"):
            user_id = channel.split(":")[-1]
            try:
                payload = json.loads(data)
                async with AsyncSessionLocal() as db:
                    await handle_roast_settled(
                        db,
                        user_id=uuid.UUID(user_id),
                        roast_instance_id=payload["roast_instance_id"],
                        turn_count=payload["turn_count"],
                        best_take=payload.get("best_take"),
                    )
            except Exception as e:
                logger.error("handle_roast_settled failed for %s: %s", user_id, e)


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
                if result.rowcount:
                    logger.info("Cleaned up %d expired provisioning sessions", result.rowcount)
        except Exception as e:
            logger.error("Session cleanup failed: %s", e)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_firebase()
    subscriber_task = asyncio.create_task(_redis_subscriber())
    cleanup_task = asyncio.create_task(_session_cleanup())
    yield
    subscriber_task.cancel()
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
