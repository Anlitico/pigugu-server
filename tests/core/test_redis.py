"""Tests for C10: Redis safe wrappers (redis_get, redis_set, redis_exists)."""

import pytest
from unittest.mock import AsyncMock, patch
from redis.exceptions import ConnectionError as RedisConnectionError

from app.core.redis import redis_get, redis_set, redis_exists


class TestRedisSafeWrappers:
    """C10: Redis operations must gracefully degrade on errors."""

    @pytest.mark.asyncio
    async def test_redis_get_returns_value_when_healthy(self):
        """redis_get returns the stored value when Redis is up."""
        with patch("app.core.redis.get_redis") as mock_get_redis:
            mock_r = AsyncMock()
            mock_r.get.return_value = '{"status":"ok"}'
            mock_get_redis.return_value = mock_r

            result = await redis_get("test:key")

        assert result == '{"status":"ok"}'
        mock_r.get.assert_awaited_once_with("test:key")

    @pytest.mark.asyncio
    async def test_redis_get_returns_none_on_error(self):
        """redis_get returns None (not exception) when Redis is down."""
        with patch("app.core.redis.get_redis") as mock_get_redis:
            mock_r = AsyncMock()
            mock_r.get.side_effect = RedisConnectionError("Connection refused")
            mock_get_redis.return_value = mock_r

            result = await redis_get("test:key")

        assert result is None

    @pytest.mark.asyncio
    async def test_redis_exists_returns_true_when_key_present(self):
        """redis_exists returns True when key exists."""
        with patch("app.core.redis.get_redis") as mock_get_redis:
            mock_r = AsyncMock()
            mock_r.exists.return_value = 1
            mock_get_redis.return_value = mock_r

            result = await redis_exists("device:online:hw:abc123")

        assert result is True

    @pytest.mark.asyncio
    async def test_redis_exists_returns_false_when_key_absent(self):
        """redis_exists returns False when key does not exist."""
        with patch("app.core.redis.get_redis") as mock_get_redis:
            mock_r = AsyncMock()
            mock_r.exists.return_value = 0
            mock_get_redis.return_value = mock_r

            result = await redis_exists("device:online:hw:missing")

        assert result is False

    @pytest.mark.asyncio
    async def test_redis_exists_returns_false_on_error(self):
        """redis_exists returns False (not exception) when Redis is down."""
        with patch("app.core.redis.get_redis") as mock_get_redis:
            mock_r = AsyncMock()
            mock_r.exists.side_effect = RedisConnectionError("Connection refused")
            mock_get_redis.return_value = mock_r

            result = await redis_exists("device:online:hw:abc123")

        assert result is False

    @pytest.mark.asyncio
    async def test_redis_set_succeeds_when_healthy(self):
        """redis_set returns True and sets value with TTL."""
        with patch("app.core.redis.get_redis") as mock_get_redis:
            mock_r = AsyncMock()
            mock_get_redis.return_value = mock_r

            result = await redis_set("test:key", "value", ex=300)

        assert result is True
        mock_r.set.assert_awaited_once_with("test:key", "value", ex=300)

    @pytest.mark.asyncio
    async def test_redis_set_returns_false_on_error(self):
        """redis_set returns False (not exception) when Redis is down."""
        with patch("app.core.redis.get_redis") as mock_get_redis:
            mock_r = AsyncMock()
            mock_r.set.side_effect = RedisConnectionError("Connection refused")
            mock_get_redis.return_value = mock_r

            result = await redis_set("test:key", "value")

        assert result is False

    @pytest.mark.asyncio
    async def test_get_device_online_status_uses_safe_wrapper(self):
        """C10: get_device_online_status uses redis_exists for safe degradation."""
        from app.modules.device.service import get_device_online_status
        with patch("app.modules.device.service.redis_exists") as mock_exists:
            mock_exists.return_value = True
            result = await get_device_online_status("test-hw-id")
        assert result is True
        mock_exists.assert_awaited_once()
