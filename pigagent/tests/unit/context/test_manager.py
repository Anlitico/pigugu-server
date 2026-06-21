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
        assert wc.raw_records == []
        assert wc.roast is None

    @pytest.mark.asyncio
    async def test_write_game_state_with_redis(self):
        import asyncio
        from unittest.mock import AsyncMock
        from context.manager import ContextManager
        redis_mock = AsyncMock()
        ctx = ContextManager(redis_client=redis_mock)
        await ctx.write_game_state(user_id="u1", state={"score": 10})
        await asyncio.sleep(0)  # let the fire-and-forget _write_game_state_redis task run
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
        from context.storage.memory import clear_all, MemoryStore
        clear_all()

        now = time.time()

        # Seed memory with a previous turn that has an active roast
        prev = ConversationRecord(
            turn_number=1, role="assistant", content="hi",
            created_at=now - 60, roast_instance_id="rid-active",
        )
        mem = MemoryStore("u1")
        mem.push_turn(prev)

        redis_mock = AsyncMock()
        redis_mock.lrange.return_value = []
        redis_mock.exists.return_value = 0
        redis_mock.get.return_value = None
        redis_mock.pipeline = MagicMock(return_value=AsyncMock())

        ctx = ContextManager(redis_client=redis_mock)

        # Add a new turn WITHOUT explicit roast_instance_id
        await ctx.add_turn(user_id="u1", role="user", content="next msg")

        # The new record should have inherited "rid-active" from history
        records = mem.get_hot_turns(10)
        assert len(records) == 2
        assert records[-1].roast_instance_id == "rid-active"
