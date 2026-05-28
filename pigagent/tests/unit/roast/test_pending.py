"""Tests for roast.pending  -  consume and write."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from roast.pending import consume


class TestConsume:
    @pytest.mark.asyncio
    async def test_none(self):
        redis = MagicMock()
        redis.get = AsyncMock(return_value=None)
        assert await consume("r1", redis) is None

    @pytest.mark.asyncio
    async def test_bytes(self):
        redis = MagicMock()
        redis.get = AsyncMock(return_value=b"test prompt")
        redis.delete = AsyncMock()
        result = await consume("r1", redis)
        assert result == "test prompt"
        redis.delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_str(self):
        redis = MagicMock()
        redis.get = AsyncMock(return_value="already str")
        redis.delete = AsyncMock()
        result = await consume("r1", redis)
        assert result == "already str"

    @pytest.mark.asyncio
    async def test_error_returns_none(self):
        redis = MagicMock()
        redis.get = AsyncMock(side_effect=Exception("boom"))
        assert await consume("r1", redis) is None


class TestWrite:
    @pytest.mark.asyncio
    async def test_writes_with_ttl(self):
        from roast.pending import write
        redis = MagicMock()
        redis.setex = AsyncMock()
        await write("r1", "prompt text", redis)
        redis.setex.assert_called_once()
        args = redis.setex.call_args[0]
        assert "r1" in args[0]
        assert args[1] == 86400
        assert args[2] == "prompt text"

    @pytest.mark.asyncio
    async def test_write_error_logged(self):
        from roast.pending import write
        redis = MagicMock()
        redis.setex = AsyncMock(side_effect=Exception("redis down"))
        await write("r1", "prompt", redis)
