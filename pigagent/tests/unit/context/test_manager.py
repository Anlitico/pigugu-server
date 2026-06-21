# tests/unit/context/test_manager.py
"""Unit tests for ContextManager  -  orchestrator, turn recording, assembly."""

import pytest


class TestContextManager:
    def test_constructor(self):
        from context.manager import ContextManager
        ctx = ContextManager("u1")
        assert ctx._redis is None

    def test_user_memory_default(self):
        from context.manager import ContextManager
        ctx = ContextManager("u1")
        assert ctx._store()._redis is None


class TestContextManagerExtended:
    """Additional ContextManager tests  -  pure helpers, Redis writes."""

    @pytest.mark.asyncio
    async def test_write_game_state_without_redis(self):
        from context.manager import ContextManager
        ctx = ContextManager("u1")
        await ctx.write_game_state(state={"score": 10})

    @pytest.mark.asyncio
    async def test_load_without_redis(self):
        from context.manager import ContextManager
        ctx = ContextManager("u1")
        msgs = await ctx.load()
        assert msgs == []

    @pytest.mark.asyncio
    async def test_assemble_without_redis(self):
        from context.manager import ContextManager
        ctx = ContextManager("u1")
        wc = await ctx.assemble()
        assert wc.user_id == "u1"
        assert wc.summary == ""
        assert wc.raw_records == []
        assert wc.roast is None

    @pytest.mark.asyncio
    async def test_write_game_state_with_redis(self):
        import asyncio
        from unittest.mock import AsyncMock
        from context.manager import ContextManager
        redis_mock = AsyncMock()
        ctx = ContextManager("u1", redis_client=redis_mock)
        await ctx.write_game_state(state={"score": 10})
        await asyncio.sleep(0)  # let the fire-and-forget _write_game_state_redis task run
        redis_mock.hset.assert_called_once()

    def test_end_roast(self):
        from context.manager import ContextManager
        import asyncio
        ctx = ContextManager("u1")
        asyncio.run(ctx.end_roast())

    def test_record_turn_without_redis(self):
        from context.manager import ContextManager
        import asyncio
        ctx = ContextManager("u1")
        asyncio.run(ctx.add_turn(
            role="user", content="hello",
        ))


class TestContextManagerAddTurn:
    """ContextManager.add_turn  -  test with mocked Redis."""

    @pytest.mark.asyncio
    async def test_add_turn_increments_counter(self):
        from unittest.mock import AsyncMock, MagicMock
        from context.manager import ContextManager
        redis_mock = AsyncMock()
        redis_mock.lrange.return_value = []
        redis_mock.exists.return_value = 0
        redis_mock.get.return_value = None
        redis_mock.pipeline = MagicMock(return_value=AsyncMock())

        ctx = ContextManager("u1", redis_client=redis_mock)
        turn_no = await ctx.add_turn(role="user", content="hello")
        assert turn_no == 1  # memory store tracked the turn number correctly

    @pytest.mark.asyncio
    async def test_add_turn_inherits_roast_instance_id_from_history(self):
        """New turns without explicit roast_instance_id must inherit it
        from the previous record in history.

        Regression test: before the fix, mem.push_turn(record) ran BEFORE
        _assign_roast_instance_id(), so the current (unassigned) record
        appeared in the history with roast=None, breaking inheritance.
        """
        import time
        from unittest.mock import AsyncMock, MagicMock
        from context.manager import ContextManager
        from context.schema import ConversationRecord

        now = time.time()

        redis_mock = AsyncMock()
        redis_mock.lrange.return_value = []
        redis_mock.exists.return_value = 0
        redis_mock.get.return_value = None
        redis_mock.pipeline = MagicMock(return_value=AsyncMock())

        ctx = ContextManager("u1", redis_client=redis_mock)

        # Seed the ContextManager's own memory with a previous turn
        # that has an active roast
        prev = ConversationRecord(
            turn_number=1, role="assistant", content="hi",
            created_at=now - 60, roast_instance_id="rid-active",
        )
        ctx._mem.push_turn(prev)

        # Add a new turn WITHOUT explicit roast_instance_id
        await ctx.add_turn(role="user", content="next msg")

        # The new record should have inherited "rid-active" from history
        records = ctx._mem.get_hot_turns(10)
        assert len(records) == 2
        assert records[-1].roast_instance_id == "rid-active"
