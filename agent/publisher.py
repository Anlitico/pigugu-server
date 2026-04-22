import json

from redis.asyncio import Redis, from_url

from app.core.config import settings

_redis: Redis | None = None


async def get_redis() -> Redis:
    global _redis
    if _redis is None:
        _redis = from_url(settings.redis_url, decode_responses=True)
    return _redis


async def publish_to_device(device_id: str, payload: dict) -> None:
    redis = await get_redis()
    await redis.publish(f"ws:device:{device_id}", json.dumps(payload))


async def publish_event(event_type: str, data: dict) -> None:
    redis = await get_redis()
    await redis.publish("agent:events", json.dumps({"type": event_type, **data}))
