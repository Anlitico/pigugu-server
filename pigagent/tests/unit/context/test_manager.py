# tests/unit/context/test_manager.py
"""Unit tests for ContextManager  -  orchestrator, turn recording, assembly."""

import pytest


class TestContextManager:
    def test_constructor(self):
        from context.manager import ContextManager
        ctx = ContextManager()
        assert ctx._redis is None

    def test_user_memory_default(self):
        from context.manager import ContextManager
        ctx = ContextManager()
        assert ctx._store("u1")._redis is None


class TestContextManagerExtended:
    """Additional ContextManager tests  -  pure helpers, Redis writes."""

    def test_record_to_msg(self):
        from context.schema import ConversationRecord
        from context.manager import _record_to_msg
        cr = ConversationRecord(turn_number=1, role="user", content="hi", created_at=100.0)
        msg = _record_to_msg(cr)
        assert msg.role == "user"
        assert msg.content == "hi"

    @pytest.mark.asyncio
    async def test_write_game_state_without_redis(self):
        from context.manager import ContextManager
        ctx = ContextManager()
        await ctx.write_game_state(user_id="u1", state={"score": 10})

    @pytest.mark.asyncio
    async def test_load_without_redis(self):
        from context.manager import ContextManager
        ctx = ContextManager()
        msgs = await ctx.load(user_id="u1")
        assert msgs == []

    @pytest.mark.asyncio
    async def test_assemble_without_redis(self):
        from context.manager import ContextManager
        ctx = ContextManager()
        wc = await ctx.assemble(user_id="u1")
        assert wc.user_id == "u1"
        assert wc.summary == ""
        assert wc.raw_turns == []
        assert wc.roast is None

    @pytest.mark.asyncio
    async def test_write_game_state_with_redis(self):
        from unittest.mock import AsyncMock
        from context.manager import ContextManager
        redis_mock = AsyncMock()
        ctx = ContextManager(redis_client=redis_mock)
        await ctx.write_game_state(user_id="u1", state={"score": 10})
        redis_mock.hset.assert_called_once()

    def test_end_roast(self):
        from context.manager import ContextManager
        import asyncio
        ctx = ContextManager()
        asyncio.run(ctx.end_roast(user_id="u1"))

    def test_record_turn_without_redis(self):
        from context.manager import ContextManager
        import asyncio
        ctx = ContextManager()
        asyncio.run(ctx.add_turn(
            user_id="u1", role="user", content="hello",
        ))


class TestContextManagerAddTurn:
    """ContextManager.add_turn  -  test with mocked Redis."""

    @pytest.mark.asyncio
    async def test_add_turn_increments_counter(self):
        from unittest.mock import AsyncMock, MagicMock
        from context.manager import ContextManager
        from context.storage.memory import clear_all
        clear_all()

        redis_mock = AsyncMock()
        redis_mock.lrange.return_value = []
        redis_mock.exists.return_value = 0
        redis_mock.get.return_value = None
        redis_mock.pipeline = MagicMock(return_value=AsyncMock())

        ctx = ContextManager(redis_client=redis_mock)
        turn_no = await ctx.add_turn(user_id="u1", role="user", content="hello")
        assert turn_no == 1  # memory store tracked the turn number correctly
