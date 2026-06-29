"""Tests for roast.activate — activate_roast, _resolve_game_mode, format_roast_message."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestResolveGameMode:
    def test_resolves_roast_together(self):
        from roast.activate import _resolve_game_mode
        mode = _resolve_game_mode("roast_together")
        assert str(mode.mode) == "roast_together"

    def test_maps_debate_directly(self):
        from roast.activate import _resolve_game_mode
        mode = _resolve_game_mode("debate")
        assert str(mode.mode) == "debate"

    def test_unknown_mode_falls_back(self):
        from roast.activate import _resolve_game_mode
        mode = _resolve_game_mode("unknown_mode")
        assert str(mode.mode) == "roast_together"


class TestBuildRoastBody:
    def test_prompt_only(self):
        from roast.activate import _build_roast_body
        gm = MagicMock()
        gm.get_system_prompt_extension = AsyncMock(return_value="")
        body = asyncio.run(_build_roast_body(
            game_mode_obj=gm, prompt="News text", prompt_store=MagicMock(),
        ))
        assert "## News Context" in body
        assert "News text" in body
        assert "## Game Mode" not in body

    def test_extension_only(self):
        from roast.activate import _build_roast_body
        gm = MagicMock()
        gm.get_system_prompt_extension = AsyncMock(return_value="Rules")
        body = asyncio.run(_build_roast_body(
            game_mode_obj=gm, prompt="", prompt_store=MagicMock(),
        ))
        assert "## Game Mode" in body
        assert "Rules" in body
        assert "## News Context" not in body

    def test_both(self):
        from roast.activate import _build_roast_body
        gm = MagicMock()
        gm.get_system_prompt_extension = AsyncMock(return_value="Rules")
        body = asyncio.run(_build_roast_body(
            game_mode_obj=gm, prompt="News", prompt_store=MagicMock(),
        ))
        assert "## News Context" in body
        assert "## Game Mode" in body

    def test_empty(self):
        from roast.activate import _build_roast_body
        gm = MagicMock()
        gm.get_system_prompt_extension = AsyncMock(return_value="")
        body = asyncio.run(_build_roast_body(
            game_mode_obj=gm, prompt="", prompt_store=MagicMock(),
        ))
        assert body == ""


class TestActivateRoast:
    @pytest.mark.asyncio
    async def test_creates_state_and_returns_body(self):
        from roast.activate import activate_roast
        mock_state = MagicMock()
        mock_state.roast_instance_id = "inst-123"
        mock_state.mode = MagicMock()
        mock_state.mode.__str__ = MagicMock(return_value="roast_together")

        redis = MagicMock()
        redis.setex = AsyncMock()

        with patch("roast.activate.RoastState") as mock_rs:
            mock_rs.start = AsyncMock(return_value=mock_state)
            instance_id, body = await activate_roast(
                user_id="u1",
                persona_id=1,
                roast_id="r1",
                game_mode="roast_together",
                prompt="test prompt",
                redis=redis,
            )

        assert instance_id == "inst-123"
        assert body.startswith("[Game Background]")
        assert "test prompt" in body
        mock_rs.start.assert_called_once()
