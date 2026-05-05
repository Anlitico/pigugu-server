"""Tests for C13: In-memory rate limiter."""

import asyncio
import pytest
from fastapi import HTTPException

from app.core.rate_limit import RateLimiter, check_session_create_limit, check_verify_limit


class TestRateLimiter:
    """C13: RateLimiter must enforce limits correctly."""

    async def _fast_forward(self, seconds: float):
        """Helper to advance the fake clock. Not needed — we test real timing."""
        pass

    @pytest.mark.asyncio
    async def test_allows_requests_within_limit(self):
        """Requests under the limit pass through."""
        limiter = RateLimiter(max_requests=3, window_seconds=10)
        for _ in range(3):
            await limiter.check("user-1")  # should not raise

    @pytest.mark.asyncio
    async def test_blocks_requests_over_limit(self):
        """Requests over the limit raise HTTP 429."""
        limiter = RateLimiter(max_requests=2, window_seconds=10)
        await limiter.check("user-2")
        await limiter.check("user-2")
        with pytest.raises(HTTPException) as exc:
            await limiter.check("user-2")
        assert exc.value.status_code == 429

    @pytest.mark.asyncio
    async def test_independent_keys(self):
        """Different keys have independent limits."""
        limiter = RateLimiter(max_requests=1, window_seconds=10)
        await limiter.check("user-a")
        await limiter.check("user-b")  # different key, should pass

    @pytest.mark.asyncio
    async def test_window_expires(self):
        """Old entries are evicted from the sliding window."""
        limiter = RateLimiter(max_requests=1, window_seconds=1)
        await limiter.check("user-3")
        # Wait for window to expire
        await asyncio.sleep(1.1)
        await limiter.check("user-3")  # should pass now

    @pytest.mark.asyncio
    async def test_concurrent_access(self):
        """Concurrent checks do not race."""
        limiter = RateLimiter(max_requests=5, window_seconds=10)
        async def check():
            await limiter.check("concurrent-user")
        tasks = [check() for _ in range(5)]
        await asyncio.gather(*tasks)
        # 6th should fail
        with pytest.raises(HTTPException):
            await limiter.check("concurrent-user")

    @pytest.mark.asyncio
    async def test_session_create_limit_uses_user_id(self):
        """Session creation rate limit is keyed by user_id."""
        # First 10 should pass
        for i in range(10):
            await check_session_create_limit(f"test-user-{i}")
        # 11th for same user should be blocked
        for _ in range(9):
            await check_session_create_limit("heavy-user")
        await check_session_create_limit("heavy-user")  # 10th — OK
        with pytest.raises(HTTPException):
            await check_session_create_limit("heavy-user")  # 11th — blocked
