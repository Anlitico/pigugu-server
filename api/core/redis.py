import logging
from redis.asyncio import Redis, from_url
from redis.exceptions import RedisError

from core.config import settings

logger = logging.getLogger(__name__)

_redis: Redis | None = None


async def get_redis() -> Redis:
    global _redis
    if _redis is None:
        _redis = from_url(settings.redis_url, decode_responses=True)
    return _redis


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None


async def redis_get(key: str) -> str | None:
    """Safe Redis GET — returns None on any error (Redis down, timeout, etc.)."""
    try:
        r = await get_redis()
        return await r.get(key)
    except RedisError as e:
        logger.error("Redis GET failed for key=%s: %s", key, e)
        return None


async def redis_exists(key: str) -> bool:
    """Safe Redis EXISTS — returns False on any error."""
    try:
        r = await get_redis()
        return await r.exists(key) > 0
    except RedisError as e:
        logger.error("Redis EXISTS failed for key=%s: %s", key, e)
        return False


async def redis_set(key: str, value: str, ex: int | None = None) -> bool:
    """Safe Redis SET — returns False on any error."""
    try:
        r = await get_redis()
        await r.set(key, value, ex=ex)
        return True
    except RedisError as e:
        logger.error("Redis SET failed for key=%s: %s", key, e)
        return False
