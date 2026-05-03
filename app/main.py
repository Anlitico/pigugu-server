import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.redis import close_redis, get_redis
from app.modules.auth.router import router as auth_router
from app.modules.device.internal_router import router as internal_device_router
from app.modules.device.router import router as device_router
from app.modules.game.router import leaderboard_router
from app.modules.game.router import router as game_router
from app.modules.news.router import router as news_router
from app.modules.push.router import router as push_router
from app.modules.push.service import init_firebase
from app.modules.ws.manager import ws_manager
from app.modules.ws.router import router as ws_router


async def _redis_subscriber() -> None:
    redis = await get_redis()
    pubsub = redis.pubsub()
    await pubsub.psubscribe("ws:device:*")
    async for message in pubsub.listen():
        if message["type"] != "pmessage":
            continue
        channel: str = message["channel"]
        device_id = channel.split(":")[-1]
        await ws_manager.broadcast(device_id, message["data"])


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_firebase()
    subscriber_task = asyncio.create_task(_redis_subscriber())
    yield
    subscriber_task.cancel()
    await close_redis()


app = FastAPI(title="Pigugu Server", version="0.1.0", lifespan=lifespan)

app.include_router(auth_router, prefix="/v1")
app.include_router(device_router, prefix="/v1")
app.include_router(internal_device_router, prefix="/v1")
app.include_router(news_router, prefix="/v1")
app.include_router(game_router, prefix="/v1")
app.include_router(leaderboard_router, prefix="/v1")
app.include_router(push_router, prefix="/v1")
app.include_router(ws_router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
