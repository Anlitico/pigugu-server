"""Tests for tools.roast — create_list_roasts_tool and create_start_roast_tool."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.agent.registry import ToolRegistry
from core.llm.types import ToolSpec


def _fake_row(roast_id, game_mode="poison_opinion", headline="H", teaser="T", created_at=None):
    return {
        "roast_id": roast_id,
        "game_mode": game_mode,
        "headline": headline,
        "teaser": teaser,
        "created_at": created_at or datetime(2026, 5, 19, 10, 0, 0, tzinfo=timezone.utc),
    }


def _fake_row_full(roast_id, game_mode="debate", prompt="P"):
    return {
        "roast_id": roast_id,
        "game_mode": game_mode,
        "prompt": prompt,
    }


# ── list_active_roasts helpers ──────────────────────────────────────────────


def _make_list_tool():
    from tools.roast import create_list_roasts_tool

    mock_conn = MagicMock()
    mock_conn.fetch = AsyncMock()
    mock_conn.close = AsyncMock()
    mock_connect = AsyncMock(return_value=mock_conn)

    tool = create_list_roasts_tool("postgresql://fake:5432/pigugu", connect=mock_connect)
    return tool, mock_conn


# ── start_roast helpers ─────────────────────────────────────────────────────


def _make_start_tool():
    from tools.roast import create_start_roast_tool, _current_user_id, _current_persona_id

    _current_user_id.set("test-user")
    _current_persona_id.set(1)

    mock_conn = MagicMock()
    mock_conn.fetchrow = AsyncMock()
    mock_conn.close = AsyncMock()
    mock_connect = AsyncMock(return_value=mock_conn)

    mock_redis = MagicMock()

    tool = create_start_roast_tool(
        "postgresql://fake:5432/pigugu",
        redis=mock_redis,
        connect=mock_connect,
    )
    return tool, mock_conn, mock_redis


# ── list_active_roasts tests ────────────────────────────────────────────────


class TestListActiveRoastsTool:
    def test_tool_name(self):
        tool, _ = _make_list_tool()
        assert tool.name == "list_active_roasts"

    def test_spec_is_tool_spec(self):
        tool, _ = _make_list_tool()
        assert isinstance(tool.spec, ToolSpec)
        assert tool.spec.name == "list_active_roasts"

    @pytest.mark.asyncio
    async def test_no_filters_returns_all(self):
        tool, conn = _make_list_tool()
        conn.fetch.return_value = [
            _fake_row("poison_001", "poison_opinion", "A"),
            _fake_row("debate_001", "debate", "B"),
        ]
        result = await tool.execute({})
        assert result["total"] == 2

    @pytest.mark.asyncio
    async def test_empty_result(self):
        tool, conn = _make_list_tool()
        conn.fetch.return_value = []
        result = await tool.execute({})
        assert result == {"total": 0, "roasts": []}

    @pytest.mark.asyncio
    async def test_connection_closed(self):
        tool, conn = _make_list_tool()
        conn.fetch.return_value = [_fake_row("r1")]
        await tool.execute({})
        assert conn.close.called

    @pytest.mark.asyncio
    async def test_connection_closed_on_error(self):
        tool, conn = _make_list_tool()
        conn.fetch.side_effect = RuntimeError("boom")
        with pytest.raises(RuntimeError):
            await tool.execute({})
        assert conn.close.called


# ── start_roast tests ───────────────────────────────────────────────────────


class TestStartRoastTool:
    def test_tool_name(self):
        tool, *_ = _make_start_tool()
        assert tool.name == "start_roast"

    def test_parameters_require_roast_id(self):
        tool, *_ = _make_start_tool()
        assert tool.parameters["required"] == ["filler_text", "roast_id"]

    @pytest.mark.asyncio
    async def test_not_found_returns_message(self):
        tool, conn, _ = _make_start_tool()
        conn.fetchrow.return_value = None
        result = await tool.execute({"roast_id": "nonexistent"})
        assert "not found" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_activates_and_injects(self):
        tool, conn, redis = _make_start_tool()
        conn.fetchrow.return_value = _fake_row_full(
            "debate_2026-05-19_001", "debate",
            "[DEBATE SCENARIO] Trump posted...",
        )

        body = "## News Context\n[DEBATE SCENARIO] Trump posted...\n\n## Game Mode\nTest rules"
        with patch("roast.activate.activate_roast", new_callable=AsyncMock) as mock_activate:
            mock_activate.return_value = ("rid-abc123", body)
            result = await tool.execute({"roast_id": "debate_2026-05-19_001"})

        # activate_roast was called with correct args
        mock_activate.assert_called_once()
        kwargs = mock_activate.call_args.kwargs
        assert kwargs["user_id"] == "test-user"
        assert kwargs["roast_id"] == "debate_2026-05-19_001"
        assert kwargs["game_mode"] == "debate"
        assert kwargs["prompt"] == "[DEBATE SCENARIO] Trump posted..."
        assert kwargs["redis"] is redis

        # Result has message + _inject
        assert "rid-abc123" in result["message"]
        assert "_inject" in result
        assert len(result["_inject"]) == 1
        assert result["_inject"][0]["role"] == "system"
        assert "News Context" in result["_inject"][0]["content"]

    @pytest.mark.asyncio
    async def test_activate_failure_returns_error(self):
        tool, conn, redis = _make_start_tool()
        conn.fetchrow.return_value = _fake_row_full("debate_001", "debate")

        with patch("roast.activate.activate_roast", new_callable=AsyncMock) as mock_activate:
            mock_activate.side_effect = RuntimeError("redis down")
            result = await tool.execute({"roast_id": "debate_001"})

        assert "Failed to start" in result["message"]
        assert "_inject" not in result

    @pytest.mark.asyncio
    async def test_empty_user_id_returns_error(self):
        from tools.roast import create_start_roast_tool, _current_user_id
        _current_user_id.set("")

        mock_redis = MagicMock()
        tool = create_start_roast_tool("postgresql://fake:5432/pigugu", redis=mock_redis)

        result = await tool.execute({"roast_id": "debate_001"})
        assert "no active user session" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_connection_closed(self):
        tool, conn, redis = _make_start_tool()
        conn.fetchrow.return_value = _fake_row_full("debate_001", "debate")

        with patch("roast.activate.activate_roast", new_callable=AsyncMock) as mock_activate:
            mock_activate.return_value = ("rid-1", "body")
            await tool.execute({"roast_id": "debate_001"})

        assert conn.close.called

    @pytest.mark.asyncio
    async def test_connection_closed_on_error(self):
        tool, conn, _ = _make_start_tool()
        conn.fetchrow.side_effect = RuntimeError("boom")
        with pytest.raises(RuntimeError):
            await tool.execute({"roast_id": "debate_001"})
        assert conn.close.called


# ── Registration ────────────────────────────────────────────────────────────


class TestRoastToolRegistration:
    def test_list_registers(self):
        tool, _ = _make_list_tool()
        registry = ToolRegistry()
        registry.register(tool)
        assert "list_active_roasts" in registry

    def test_start_registers(self):
        tool, *_ = _make_start_tool()
        registry = ToolRegistry()
        registry.register(tool)
        assert "start_roast" in registry

    def test_both_tools(self):
        list_tool, _ = _make_list_tool()
        start_tool, *_ = _make_start_tool()
        registry = ToolRegistry()
        registry.register(list_tool)
        registry.register(start_tool)
        assert len(registry) == 2
