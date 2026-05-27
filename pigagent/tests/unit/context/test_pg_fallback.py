# tests/unit/context/test_pg_fallback.py
"""Unit tests for PG fallback — summary persistence, turn recovery, assemble recovery."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from context.storage.pg import PgStorage
from context.schema import SummaryRow, ConversationRecord, SummaryRecord, UserMemory
from context.manager import ContextManager


# ── Helpers ────────────────────────────────────────────────────────────────


class _MockPool:
    """Async context manager that yields a mock connection, mimicking _connect()."""

    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *args):
        pass


def _mock_conn_fetchrow(return_row=None):
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=return_row)
    conn.execute = AsyncMock()
    return _MockPool(conn)


def _mock_conn_fetch(return_rows=None):
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=return_rows or [])
    conn.execute = AsyncMock()
    return _MockPool(conn)


# ── SummaryRow tests ───────────────────────────────────────────────────────


class TestSummaryRow:
    def test_defaults(self):
        row = SummaryRow(user_id="u1", end_turn=5)
        assert row.user_id == "u1"
        assert row.end_turn == 5
        assert row.l2_profile == ""
        assert row.l3_session == ""
        assert row.l4_roast == ""

    def test_full_row(self):
        row = SummaryRow(
            user_id="u1", end_turn=10,
            l2_profile="profile text",
            l3_session="session summary",
            l4_roast="roast summary",
            roast_id="rid-1", model_used="qwen-plus",
        )
        assert row.l2_profile == "profile text"
        assert row.roast_id == "rid-1"


# ── PgStorage write / read / recover tests ──────────────────────────────────


class TestPgWriteSummaryRow:
    @pytest.mark.asyncio
    async def test_write_no_pg_pool_returns_early(self):
        pg = PgStorage("u1", pg_pool=None)
        # Should not raise
        await pg.write_summary_row(5, l3_session="summary")

    @pytest.mark.asyncio
    async def test_write_inserts_row(self):
        pool = _mock_conn_fetchrow()
        with patch("context.storage.pg._connect", return_value=pool):
            pg = PgStorage("u1", pg_pool="postgresql://test")
            await pg.write_summary_row(
                5, l2_profile="p", l3_session="s", l4_roast="r",
                roast_id="rid", model_used="qwen",
            )
            pool._conn.execute.assert_called_once()
            args = pool._conn.execute.call_args
            assert args[0][0].startswith("INSERT INTO context_summaries")


class TestPgReadLatestSummary:
    @pytest.mark.asyncio
    async def test_read_no_pg_pool_returns_none(self):
        pg = PgStorage("u1", pg_pool=None)
        assert await pg.read_latest_summary() is None

    @pytest.mark.asyncio
    async def test_read_empty_returns_none(self):
        pool = _mock_conn_fetchrow(return_row=None)
        with patch("context.storage.pg._connect", return_value=pool):
            pg = PgStorage("u1", pg_pool="postgresql://test")
            assert await pg.read_latest_summary() is None

    @pytest.mark.asyncio
    async def test_read_returns_summary_row(self):
        row_data = {
            "user_id": "u1", "end_turn": 10,
            "l2_profile": "profile", "l3_session": "session",
            "l4_roast": "", "roast_id": "", "model_used": "",
        }
        pool = _mock_conn_fetchrow(return_row=row_data)
        with patch("context.storage.pg._connect", return_value=pool):
            pg = PgStorage("u1", pg_pool="postgresql://test")
            row = await pg.read_latest_summary()
            assert row is not None
            assert row.end_turn == 10
            assert row.l2_profile == "profile"
            assert row.l3_session == "session"


class TestPgRecoverTurns:
    @pytest.mark.asyncio
    async def test_recover_no_pg_pool_returns_empty(self):
        pg = PgStorage("u1", pg_pool=None)
        assert await pg.recover_turns() == []

    @pytest.mark.asyncio
    async def test_recover_empty_returns_empty(self):
        pool = _mock_conn_fetch(return_rows=[])
        with patch("context.storage.pg._connect", return_value=pool):
            pg = PgStorage("u1", pg_pool="postgresql://test")
            assert await pg.recover_turns() == []

    @pytest.mark.asyncio
    async def test_recover_returns_conversation_records(self):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        rows = [
            {
                "turn_number": 1, "role": "user", "content": "hello",
                "tool_calls": None, "tool_call_id": None, "name": None,
                "partial": False, "roast_instance_id": None, "created_at": now,
            },
            {
                "turn_number": 2, "role": "assistant", "content": "hi there",
                "tool_calls": None, "tool_call_id": None, "name": None,
                "partial": False, "roast_instance_id": None, "created_at": now,
            },
        ]
        pool = _mock_conn_fetch(return_rows=rows)
        with patch("context.storage.pg._connect", return_value=pool):
            pg = PgStorage("u1", pg_pool="postgresql://test")
            records = await pg.recover_turns()
            assert len(records) == 2
            assert records[0].turn_number == 1
            assert records[0].role == "user"
            assert records[1].turn_number == 2

    @pytest.mark.asyncio
    async def test_recover_respects_after_turn(self):
        pool = _mock_conn_fetch(return_rows=[])
        with patch("context.storage.pg._connect", return_value=pool):
            pg = PgStorage("u1", pg_pool="postgresql://test")
            await pg.recover_turns(after_turn=5, limit=50)
            args = pool._conn.fetch.call_args
            assert args[0][2] == 5   # after_turn
            assert args[0][3] == 50  # limit


# ── ContextManager assemble PG fallback tests ──────────────────────────────


class TestAssembleFallback:
    @pytest.mark.asyncio
    async def test_assemble_falls_back_to_pg_when_redis_empty(self):
        """When Redis returns no turns and no summary, PG is queried."""
        redis = MagicMock()
        redis.get = AsyncMock(return_value=None)
        redis.lrange = AsyncMock(return_value=[])
        redis.hgetall = AsyncMock(return_value={})
        redis.exists = AsyncMock(return_value=0)
        redis.hset = AsyncMock()
        redis.set = AsyncMock()
        redis.delete = AsyncMock()
        redis.pipeline = MagicMock()

        pg_pool = "postgresql://test"

        mgr = ContextManager(redis_client=redis, pg_pool=pg_pool)

        row_data = {
            "user_id": "u1", "end_turn": 10,
            "l2_profile": "User is a developer",
            "l3_session": "Previous conversation about Python",
            "l4_roast": "", "roast_id": "", "model_used": "qwen",
        }
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        turns_data = [
            {
                "turn_number": 11, "role": "user", "content": "latest message",
                "tool_calls": None, "tool_call_id": None, "name": None,
                "partial": False, "roast_instance_id": None, "created_at": now,
            },
        ]

        # Two separate mock connections — one per _connect() call
        pool1 = _mock_conn_fetchrow(return_row=row_data)
        pool2 = _mock_conn_fetch(return_rows=turns_data)

        with patch("context.storage.pg._connect", side_effect=[pool1, pool2]):
            wc = await mgr.assemble("u1")

            assert wc.summary == "Previous conversation about Python"
            assert wc.summary_end_turn == 10
            assert wc.user_memory is not None
            assert wc.user_memory.profile_summary == "User is a developer"
            assert len(wc.raw_turns) == 1
            assert wc.raw_turns[0].role == "user"

    @pytest.mark.asyncio
    async def test_assemble_no_redis_no_pg_returns_empty_context(self):
        """When both Redis and PG are empty, return empty WorkingContext."""
        redis = MagicMock()
        redis.get = AsyncMock(return_value=None)
        redis.lrange = AsyncMock(return_value=[])
        redis.hgetall = AsyncMock(return_value={})
        redis.exists = AsyncMock(return_value=0)

        mgr = ContextManager(redis_client=redis, pg_pool=None)

        wc = await mgr.assemble("u1")
        assert wc.summary == ""
        assert wc.raw_turns == []
        assert wc.user_memory is not None
        assert wc.user_memory.user_id == "u1"

    @pytest.mark.asyncio
    async def test_assemble_redis_has_data_no_fallback(self):
        """When Redis has turns, PG is NOT queried."""
        redis = MagicMock()
        redis.get = AsyncMock(return_value=None)
        redis.lrange = AsyncMock(return_value=[
            b'{"turn":1,"role":"user","content":"hello","ts":1.0}',
        ])
        redis.hgetall = AsyncMock(return_value={})
        redis.exists = AsyncMock(return_value=0)
        redis.pipeline = MagicMock()

        mgr = ContextManager(redis_client=redis, pg_pool=None)
        wc = await mgr.assemble("u1")

        assert len(wc.raw_turns) == 1
        assert wc.raw_turns[0].role == "user"


class TestRewarmRedis:
    @pytest.mark.asyncio
    async def test_rewarm_writes_all_data(self):
        redis = MagicMock()
        redis.set = AsyncMock()
        redis.pipeline = MagicMock()
        pipe = MagicMock()
        pipe.rpush = MagicMock()
        pipe.ltrim = MagicMock()
        pipe.execute = AsyncMock()
        redis.pipeline.return_value.__aenter__ = AsyncMock(return_value=pipe)
        redis.pipeline.return_value.__aexit__ = AsyncMock()

        mgr = ContextManager(redis_client=redis, pg_pool=None)
        data = {"end_turn": 5, "l2_profile": "profile text",
                "l3_session": "summary text", "l4_roast": "", "roast_id": ""}
        records = [
            ConversationRecord(turn_number=6, role="user", content="hi", created_at=1.0),
            ConversationRecord(turn_number=7, role="assistant", content="hey", created_at=2.0),
        ]

        await mgr._rewarm_redis("u1", data, records)

        redis.set.assert_called_once()
        assert pipe.rpush.call_count == 2

    @pytest.mark.asyncio
    async def test_rewarm_handles_empty_data(self):
        redis = MagicMock()
        redis.set = AsyncMock()
        redis.pipeline = MagicMock()
        pipe = MagicMock()
        pipe.rpush = MagicMock()
        pipe.ltrim = MagicMock()
        pipe.execute = AsyncMock()
        redis.pipeline.return_value.__aenter__ = AsyncMock(return_value=pipe)
        redis.pipeline.return_value.__aexit__ = AsyncMock()

        mgr = ContextManager(redis_client=redis, pg_pool=None)
        data = {"end_turn": 0, "l2_profile": "", "l3_session": "", "l4_roast": "", "roast_id": ""}
        records = [ConversationRecord(turn_number=1, role="user", content="hi", created_at=1.0)]

        await mgr._rewarm_redis("u1", data, records)

        redis.set.assert_called_once()  # write_summaries always writes
        pipe.rpush.assert_called_once()
