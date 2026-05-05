"""Simple in-memory rate limiter for provisioning endpoints.

Uses a sliding-window counter per key. Not distributed — for multi-worker
deployments, replace with Redis-backed limiter (e.g. slowapi + Redis).
"""

import asyncio
import time
from collections import defaultdict

from fastapi import HTTPException, status


class RateLimiter:
    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._buckets: dict[str, list[float]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def check(self, key: str) -> None:
        now = time.monotonic()
        async with self._lock:
            bucket = self._buckets[key]
            # Evict old entries
            cutoff = now - self.window_seconds
            while bucket and bucket[0] < cutoff:
                bucket.pop(0)
            if len(bucket) >= self.max_requests:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Rate limit exceeded. Please retry later.",
                )
            bucket.append(now)


# Per-endpoint limiters
_session_create_limiter = RateLimiter(max_requests=10, window_seconds=3600)      # 10 / hour / user
_verify_limiter = RateLimiter(max_requests=5, window_seconds=300)                # 5 / 5 min / session
_mqtt_creds_limiter = RateLimiter(max_requests=5, window_seconds=300)            # 5 / 5 min / session


async def check_session_create_limit(user_id: str) -> None:
    await _session_create_limiter.check(f"session_create:{user_id}")


async def check_verify_limit(session_id: str) -> None:
    await _verify_limiter.check(f"verify:{session_id}")


async def check_mqtt_creds_limit(session_id: str) -> None:
    await _mqtt_creds_limiter.check(f"mqtt_creds:{session_id}")
