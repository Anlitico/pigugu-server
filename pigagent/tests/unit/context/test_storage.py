# tests/unit/context/test_storage.py
"""Unit tests for context storage — RedisKeys, RedisStorage, PgStorage."""

import json

import pytest

from context.storage.redis import RedisKeys


# ═══════════════════════════════════════════════════════════════════════════════
# RedisKeys
# ═══════════════════════════════════════════════════════════════════════════════

class TestRedisKeys:
    def test_turns(self):
        assert RedisKeys.turns("u1") == "ctx:u1:turns"

    def test_compressing(self):
        assert RedisKeys.compressing("u1") == "ctx:u1:compressing"

    def test_summary(self):
        assert RedisKeys.summary("u1") == "ctx:u1:summary"

    def test_game_state(self):
        assert RedisKeys.game_state("u1") == "ctx:u1:game_state"

    def test_user_memory(self):
        assert RedisKeys.user_memory("u1") == "pigugu:user:u1:memory"

    def test_roast_prompt(self):
        assert RedisKeys.roast_prompt("u1") == "ctx:u1:roast:prompt"

    def test_roast_turns(self):
        assert RedisKeys.roast_turns("u1") == "ctx:u1:roast:turns"

    def test_roast_summary(self):
        assert RedisKeys.roast_summary("u1") == "ctx:u1:roast:summary"

    def test_roast_meta(self):
        assert RedisKeys.roast_meta("u1") == "ctx:u1:roast:meta"


# ═══════════════════════════════════════════════════════════════════════════════
# RedisStorage — happy path with mocked Redis
# ═══════════════════════════════════════════════════════════════════════════════

class TestRedisStorage:
    """RedisStorage — all methods with mocked Redis client."""

    @pytest.fixture
    def redis_mock(self):
        from unittest.mock import AsyncMock, MagicMock
        mock = AsyncMock()
        mock.pipeline = MagicMock(return_value=AsyncMock())
        return mock

    @pytest.fixture
    def store(self, redis_mock):
        from context.storage.redis import RedisStorage
        return RedisStorage("u1", redis_mock)

    # ── get_hot_turns ──

    @pytest.mark.asyncio
    async def test_get_hot_turns_no_redis(self):
        from context.storage.redis import RedisStorage
        store = RedisStorage("u1", None)
        result = await store.get_hot_turns(10)
        assert result == []

    @pytest.mark.asyncio
    async def test_get_hot_turns_empty(self, store, redis_mock):
        redis_mock.lrange.return_value = []
        result = await store.get_hot_turns(10)
        assert result == []

    @pytest.mark.asyncio
    async def test_get_hot_turns_returns_records(self, store, redis_mock):
        import json
        d = json.dumps({"turn": 1, "role": "user", "content": "hi", "ts": 100.0})
        redis_mock.lrange.return_value = [d.encode()]
        result = await store.get_hot_turns(10)
        assert len(result) == 1
        assert result[0].turn_number == 1
        assert result[0].role == "user"

    @pytest.mark.asyncio
    async def test_get_hot_turns_respects_after_anchor(self, store, redis_mock):
        import json
        d1 = json.dumps({"turn": 1, "role": "user", "content": "a", "ts": 100.0})
        d2 = json.dumps({"turn": 2, "role": "user", "content": "b", "ts": 200.0})
        redis_mock.lrange.return_value = [d1.encode(), d2.encode()]
        result = await store.get_hot_turns(10, after_anchor=1)
        assert len(result) == 1
        assert result[0].turn_number == 2

    # ── get_all_turns_with_numbers ──

    @pytest.mark.asyncio
    async def test_get_all_turns_no_redis(self):
        from context.storage.redis import RedisStorage
        store = RedisStorage("u1", None)
        result = await store.get_all_turns_with_numbers()
        assert result == []

    @pytest.mark.asyncio
    async def test_get_all_turns_returns_all(self, store, redis_mock):
        import json
        d1 = json.dumps({"turn": 1, "role": "user", "content": "a", "ts": 100.0})
        d2 = json.dumps({"turn": 2, "role": "assistant", "content": "b", "ts": 200.0})
        redis_mock.lrange.return_value = [d1.encode(), d2.encode()]
        result = await store.get_all_turns_with_numbers()
        assert len(result) == 2

    # ── get_last_turn_number ──

    @pytest.mark.asyncio
    async def test_get_last_turn_number_empty(self, store, redis_mock):
        redis_mock.lrange.return_value = []
        result = await store.get_last_turn_number()
        assert result == 0

    @pytest.mark.asyncio
    async def test_get_last_turn_number_returns_max(self, store, redis_mock):
        import json
        d = json.dumps({"turn": 42, "role": "assistant", "content": "ok", "ts": 500.0})
        redis_mock.lrange.return_value = [d.encode()]
        result = await store.get_last_turn_number()
        assert result == 42

    # ── has_turns ──

    @pytest.mark.asyncio
    async def test_has_turns_no_redis(self):
        from context.storage.redis import RedisStorage
        store = RedisStorage("u1", None)
        assert await store.has_turns() is False

    @pytest.mark.asyncio
    async def test_has_turns_true(self, store, redis_mock):
        redis_mock.exists.return_value = 1
        assert await store.has_turns() is True

    @pytest.mark.asyncio
    async def test_has_turns_false(self, store, redis_mock):
        redis_mock.exists.return_value = 0
        assert await store.has_turns() is False

    # ── is_compressing / set_compressing ──

    @pytest.mark.asyncio
    async def test_is_compressing_no_redis(self):
        from context.storage.redis import RedisStorage
        store = RedisStorage("u1", None)
        assert await store.is_compressing() is False

    @pytest.mark.asyncio
    async def test_is_compressing_true(self, store, redis_mock):
        redis_mock.get.return_value = b"1"
        assert await store.is_compressing() is True

    @pytest.mark.asyncio
    async def test_is_compressing_false(self, store, redis_mock):
        redis_mock.get.return_value = None
        assert await store.is_compressing() is False

    @pytest.mark.asyncio
    async def test_set_compressing_true(self, store, redis_mock):
        await store.set_compressing(True)
        redis_mock.set.assert_called_once()

    @pytest.mark.asyncio
    async def test_set_compressing_false(self, store, redis_mock):
        await store.set_compressing(False)
        redis_mock.delete.assert_called_once()

    # ── read_summary / write_summary ──

    @pytest.mark.asyncio
    async def test_read_summary_none_when_no_redis(self):
        from context.storage.redis import RedisStorage
        store = RedisStorage("u1", None)
        assert await store.read_summary() is None

    @pytest.mark.asyncio
    async def test_read_summary_none_when_no_data(self, store, redis_mock):
        redis_mock.get.return_value = None
        assert await store.read_summary() is None

    @pytest.mark.asyncio
    async def test_read_summary_returns_record(self, store, redis_mock):
        import json
        raw = json.dumps({"text": "summary text", "end_turn": 10})
        redis_mock.get.return_value = raw.encode()
        sr = await store.read_summary()
        assert sr is not None
        assert sr.text == "summary text"
        assert sr.end_turn == 10

    @pytest.mark.asyncio
    async def test_write_summary(self, store, redis_mock):
        from context.schema import SummaryRecord
        sr = SummaryRecord(text="summary", end_turn=5)
        await store.write_summary(sr)
        redis_mock.set.assert_called_once()

    # ── read_game_state ──

    @pytest.mark.asyncio
    async def test_read_game_state_no_redis(self):
        from context.storage.redis import RedisStorage
        store = RedisStorage("u1", None)
        assert await store.read_game_state() == {}

    @pytest.mark.asyncio
    async def test_read_game_state_returns_dict(self, store, redis_mock):
        redis_mock.hgetall.return_value = {b"score": b"100"}
        result = await store.read_game_state()
        assert result == {"score": "100"}

    # ── load_user_memory / write_user_memory ──

    @pytest.mark.asyncio
    async def test_load_user_memory_no_redis(self):
        from context.storage.redis import RedisStorage
        store = RedisStorage("u1", None)
        assert await store.load_user_memory() is None

    @pytest.mark.asyncio
    async def test_load_user_memory_returns_um(self, store, redis_mock):
        import json
        redis_mock.hgetall.return_value = {
            b"profile_summary": b"user profile",
            b"stats_json": json.dumps({"turns": 5}),
        }
        um = await store.load_user_memory()
        assert um is not None
        assert um.profile_summary == "user profile"

    @pytest.mark.asyncio
    async def test_write_user_memory(self, store, redis_mock):
        from context.schema import UserMemory
        um = UserMemory(user_id="u1", profile_summary="test")
        await store.write_user_memory(um)
        redis_mock.hset.assert_called_once()

    # ── push_turn ──

    @pytest.mark.asyncio
    async def test_push_turn_no_redis(self):
        from context.storage.redis import RedisStorage
        store = RedisStorage("u1", None)
        await store.push_turn("{}")

    @pytest.mark.asyncio
    async def test_push_turn_succeeds(self, store, redis_mock):
        await store.push_turn('{"turn":1}')
        redis_mock.pipeline.assert_called_once()

    # ── Roast methods ──

    @pytest.mark.asyncio
    async def test_read_roast_prompt_empty(self, store, redis_mock):
        redis_mock.get.return_value = None
        assert await store.read_roast_prompt() == ""

    @pytest.mark.asyncio
    async def test_read_roast_prompt_returns_str(self, store, redis_mock):
        redis_mock.get.return_value = b"game rules here"
        assert await store.read_roast_prompt() == "game rules here"

    @pytest.mark.asyncio
    async def test_read_roast_summary_empty(self, store, redis_mock):
        redis_mock.get.return_value = None
        assert await store.read_roast_summary() == ""

    @pytest.mark.asyncio
    async def test_write_roast_summary(self, store, redis_mock):
        await store.write_roast_summary("roast summary")
        redis_mock.set.assert_called_once()

    @pytest.mark.asyncio
    async def test_read_roast_meta_returns_dict(self, store, redis_mock):
        redis_mock.hgetall.return_value = {b"roast_id": b"rx", b"turn_count": b"3"}
        result = await store.read_roast_meta()
        assert result == {"roast_id": "rx", "turn_count": "3"}

    @pytest.mark.asyncio
    async def test_write_roast_meta(self, store, redis_mock):
        await store.write_roast_meta({"roast_id": "rx"})
        redis_mock.hset.assert_called_once()

    @pytest.mark.asyncio
    async def test_read_roast_turns_raw(self, store, redis_mock):
        redis_mock.lrange.return_value = [b"t1", b"t2"]
        result = await store.read_roast_turns_raw()
        assert result == [b"t1", b"t2"]

    @pytest.mark.asyncio
    async def test_delete_roast_keys(self, store, redis_mock):
        await store.delete_roast_keys()
        redis_mock.delete.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════════
# RedisStorage — exception handling
# ═══════════════════════════════════════════════════════════════════════════════

class TestRedisStorageExceptionHandling:
    """RedisStorage — exception handling returns safe defaults."""

    @pytest.fixture
    def broken_redis(self):
        from unittest.mock import AsyncMock
        mock = AsyncMock()
        mock.lrange.side_effect = Exception("connection lost")
        mock.get.side_effect = Exception("connection lost")
        mock.exists.side_effect = Exception("connection lost")
        mock.hgetall.side_effect = Exception("connection lost")
        mock.set.side_effect = Exception("connection lost")
        mock.delete.side_effect = Exception("connection lost")
        mock.hset.side_effect = Exception("connection lost")
        return mock

    @pytest.fixture
    def safe_store(self, broken_redis):
        from context.storage.redis import RedisStorage
        return RedisStorage("u1", broken_redis)

    @pytest.mark.asyncio
    async def test_get_hot_turns_returns_empty(self, safe_store):
        result = await safe_store.get_hot_turns(10)
        assert result == []

    @pytest.mark.asyncio
    async def test_get_all_turns_returns_empty(self, safe_store):
        result = await safe_store.get_all_turns_with_numbers()
        assert result == []

    @pytest.mark.asyncio
    async def test_has_turns_returns_false(self, safe_store):
        assert await safe_store.has_turns() is False

    @pytest.mark.asyncio
    async def test_is_compressing_returns_false(self, safe_store):
        assert await safe_store.is_compressing() is False

    @pytest.mark.asyncio
    async def test_read_summary_returns_none(self, safe_store):
        assert await safe_store.read_summary() is None

    @pytest.mark.asyncio
    async def test_read_game_state_returns_empty(self, safe_store):
        assert await safe_store.read_game_state() == {}

    @pytest.mark.asyncio
    async def test_load_user_memory_returns_none(self, safe_store):
        assert await safe_store.load_user_memory() is None

    @pytest.mark.asyncio
    async def test_set_compressing_no_raise(self, safe_store):
        await safe_store.set_compressing(True)

    @pytest.mark.asyncio
    async def test_write_summary_no_raise(self, safe_store):
        from context.schema import SummaryRecord
        await safe_store.write_summary(SummaryRecord(text="x"))

    @pytest.mark.asyncio
    async def test_write_user_memory_no_raise(self, safe_store):
        from context.schema import UserMemory
        await safe_store.write_user_memory(UserMemory(user_id="u1"))

    @pytest.mark.asyncio
    async def test_push_turn_no_raise(self, safe_store):
        await safe_store.push_turn("{}")

    @pytest.mark.asyncio
    async def test_read_roast_prompt_returns_empty(self, safe_store):
        assert await safe_store.read_roast_prompt() == ""

    @pytest.mark.asyncio
    async def test_read_roast_summary_returns_empty(self, safe_store):
        assert await safe_store.read_roast_summary() == ""

    @pytest.mark.asyncio
    async def test_write_roast_summary_no_raise(self, safe_store):
        await safe_store.write_roast_summary("summary")

    @pytest.mark.asyncio
    async def test_read_roast_meta_returns_empty(self, safe_store):
        assert await safe_store.read_roast_meta() == {}

    @pytest.mark.asyncio
    async def test_write_roast_meta_no_raise(self, safe_store):
        await safe_store.write_roast_meta({})

    @pytest.mark.asyncio
    async def test_read_roast_turns_raw_returns_empty(self, safe_store):
        assert await safe_store.read_roast_turns_raw() == []

    @pytest.mark.asyncio
    async def test_delete_roast_keys_no_raise(self, safe_store):
        await safe_store.delete_roast_keys()


# ═══════════════════════════════════════════════════════════════════════════════
# PgStorage
# ═══════════════════════════════════════════════════════════════════════════════

class TestPgStorage:
    """PgStorage — all methods with mocked PG pool."""

    @pytest.fixture
    def pg_mock(self):
        from unittest.mock import AsyncMock, MagicMock
        mock = AsyncMock()
        mock.acquire = MagicMock(return_value=AsyncMock())
        return mock

    @pytest.fixture
    def pg_store(self, pg_mock):
        from context.storage.pg import PgStorage
        return PgStorage("u1", pg_mock)

    # ── No-pool short-circuits ──

    def test_no_pool_returns(self):
        from context.storage.pg import PgStorage
        store = PgStorage("u1", None)
        from core.llm.types import Message
        import asyncio

        async def run():
            await store.flush_one(1, Message.user("hi"), None)
            await store.flush_buffer([(1, Message.user("hi"), None)])
            await store.persist_turns([(1, Message.user("hi"), None)])
            assert await store.recover_turn_counter() == 0
            await store.persist_facts([{"fact": "x", "category": "y"}])
            assert await store.read_new_facts() == []
            profile, ts = await store.read_profile()
            assert profile == ""
            assert ts is None
            await store.upsert_profile("profile")
        asyncio.run(run())

    # ── recover_turn_counter ──

    @pytest.mark.asyncio
    async def test_recover_turn_counter_returns_max(self, pg_store, pg_mock):
        conn = pg_mock.acquire.return_value.__aenter__.return_value
        conn.fetchrow.return_value = [55]
        result = await pg_store.recover_turn_counter()
        assert result == 55

    @pytest.mark.asyncio
    async def test_recover_turn_counter_empty_db(self, pg_store, pg_mock):
        conn = pg_mock.acquire.return_value.__aenter__.return_value
        conn.fetchrow.return_value = None
        result = await pg_store.recover_turn_counter()
        assert result == 0

    # ── read_new_facts ──

    @pytest.mark.asyncio
    async def test_read_new_facts_returns_list(self, pg_store, pg_mock):
        conn = pg_mock.acquire.return_value.__aenter__.return_value
        conn.fetch.return_value = [
            {"fact": "loves pizza", "category": "food"},
            {"fact": "lives in SH", "category": "location"},
        ]
        result = await pg_store.read_new_facts()
        assert len(result) == 2
        assert "loves pizza (food)" in result
        assert "lives in SH (location)" in result

    # ── read_profile ──

    @pytest.mark.asyncio
    async def test_read_profile_returns_data(self, pg_store, pg_mock):
        from datetime import datetime, timezone
        conn = pg_mock.acquire.return_value.__aenter__.return_value
        now = datetime.now(timezone.utc)
        conn.fetchrow.return_value = {"profile_summary": "User profile", "updated_at": now}
        profile, ts = await pg_store.read_profile()
        assert profile == "User profile"
        assert ts == now

    @pytest.mark.asyncio
    async def test_read_profile_empty_row(self, pg_store, pg_mock):
        conn = pg_mock.acquire.return_value.__aenter__.return_value
        conn.fetchrow.return_value = None
        profile, ts = await pg_store.read_profile()
        assert profile == ""
        assert ts is None

    # ── flush_one ──

    @pytest.mark.asyncio
    async def test_flush_one_inserts(self, pg_store, pg_mock):
        from core.llm.types import Message
        conn = pg_mock.acquire.return_value.__aenter__.return_value
        await pg_store.flush_one(1, Message.user("hello"), "rx")
        conn.execute.assert_called_once()

    # ── _serialize_tool_calls ──

    def test_serialize_tool_calls_none(self):
        from context.storage.pg import _serialize_tool_calls
        assert _serialize_tool_calls(None) is None

    def test_serialize_tool_calls_empty(self):
        from context.storage.pg import _serialize_tool_calls
        assert _serialize_tool_calls([]) is None

    def test_serialize_tool_calls_returns_json(self):
        from context.storage.pg import _serialize_tool_calls
        from core.llm.types import ToolCall
        tcs = [ToolCall(id="c1", name="f", arguments="{}")]
        result = _serialize_tool_calls(tcs)
        import json
        assert result is not None
        data = json.loads(result)
        assert data[0]["id"] == "c1"
        assert data[0]["name"] == "f"
