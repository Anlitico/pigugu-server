"""Tests for roast.base  -  Trigger and GameMode base class."""

from roast.base import Trigger, GameMode
from roast.state import RoastState
from roast.types import Mode


class TestTrigger:
    def test_static_prompt(self):
        t = Trigger(
            name="test",
            check=lambda s, r: True,
            prompt="hello",
        )
        assert t.name == "test"
        assert t.prompt == "hello"

    def test_callable_prompt(self):
        t = Trigger(
            name="dyn",
            check=lambda s, r: True,
            prompt=lambda s: f"turn {s.turn_count}",
        )
        state = RoastState.__new__(RoastState)
        state.turn_count = 3
        assert callable(t.prompt)
        assert t.prompt(state) == "turn 3"

    def test_check(self):
        t = Trigger(
            name="always",
            check=lambda s, r: True,
            prompt="ok",
        )
        assert t.check(None, [])  # type: ignore[arg-type]

        t2 = Trigger(
            name="never",
            check=lambda s, r: False,
            prompt="ok",
        )
        assert not t2.check(None, [])  # type: ignore[arg-type]


class _MinimalMode(GameMode):
    mode = Mode.ROAST_TOGETHER
    max_turns = 3

    @property
    def system_prompt_extension(self) -> str:
        return "minimal rules"

    @property
    def director_prompt(self) -> str:
        return "You are a test director. Default to action:none."


class TestGameModeBase:
    def test_default_triggers_has_ending(self):
        mode = _MinimalMode()
        triggers = mode.triggers
        assert len(triggers) == 1
        assert triggers[0].name == "ending_max_turns"

    def test_default_ending_trigger_fires(self):
        mode = _MinimalMode()
        state = RoastState.__new__(RoastState)
        state.user_id = "u1"
        state.turn_count = 3
        assert mode.triggers[0].check(state, [])

    def test_default_ending_not_fires_early(self):
        mode = _MinimalMode()
        state = RoastState.__new__(RoastState)
        state.user_id = "u1"
        state.turn_count = 1
        assert not mode.triggers[0].check(state, [])



class TestGameModeTick:
    """End-to-end tick() flow using a real mode (RoastTogetherMode)."""

    def _state(self, **kw):
        from roast.types import Phase
        s = RoastState.__new__(RoastState)
        s.user_id = "u1"
        s.persona_id = 1
        s.roast_id = "n1"
        s.mode = Mode.ROAST_TOGETHER
        s.roast_instance_id = "test-id"
        s.phase = kw.pop("phase", Phase.ACTIVE)
        s.turn_count = kw.pop("turn_count", 0)
        s.started_at = kw.pop("started_at", 0.0)
        s.extra = kw.pop("extra", {})
        return s

    def test_ending_on_max_turns(self):
        from unittest.mock import MagicMock
        from roast.modes.roast_together import RoastTogetherMode
        import asyncio

        redis = MagicMock()
        state = self._state(turn_count=7)
        mode = RoastTogetherMode()

        result = asyncio.run(mode.tick(state, records=[], redis=redis))
        assert result is not None
        assert state.turn_count == 8

    def test_no_transition_mid_game(self):
        from unittest.mock import MagicMock
        from roast.modes.roast_together import RoastTogetherMode
        import asyncio

        redis = MagicMock()
        state = self._state(turn_count=1)
        mode = RoastTogetherMode()

        result = asyncio.run(mode.tick(state, records=[], redis=redis))
        assert result is None
        assert state.turn_count == 2

    def test_skips_if_not_active(self):
        from unittest.mock import MagicMock
        from roast.types import Phase
        from roast.modes.roast_together import RoastTogetherMode
        import asyncio

        redis = MagicMock()
        state = self._state(phase=Phase.CLOSING, turn_count=5)
        mode = RoastTogetherMode()

        result = asyncio.run(mode.tick(state, records=[], redis=redis))
        assert result is None
        assert state.turn_count == 5  # unchanged


class TestGameModeTickDirector:
    """Tests for the refactored tick() — close decoupled from inject."""

    def _state(self, **kw):
        from roast.types import Phase
        s = RoastState.__new__(RoastState)
        s.user_id = "u1"
        s.persona_id = 1
        s.roast_id = "n1"
        s.mode = Mode.ROAST_TOGETHER
        s.roast_instance_id = "test-id"
        s.phase = kw.pop("phase", Phase.ACTIVE)
        s.turn_count = kw.pop("turn_count", 0)
        s.started_at = kw.pop("started_at", 0.0)
        s.extra = kw.pop("extra", {})
        return s

    def test_close_without_inject_gets_default_prompt(self):
        """Director close=true action=none → phase=CLOSING + default prompt."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch
        from roast.types import Phase
        from roast.modes.roast_together import RoastTogetherMode

        redis = MagicMock()
        redis.setex = AsyncMock()
        state = self._state(turn_count=3)
        mode = RoastTogetherMode()

        with patch.object(mode, '_direct', new_callable=AsyncMock) as mock_direct:
            mock_direct.return_value = {
                "action": "none", "best_take": None,
                "prompt": None, "close": True,
            }
            result = asyncio.run(mode.tick(state, records=[], redis=redis))

        assert result is not None
        assert "GAME IS OVER" in result
        assert state.phase == Phase.CLOSING
        # Verify prompt was written to pending (setex called for pending + state.save)
        pending_call = redis.setex.call_args_list[0]
        assert "pending_prompt" in pending_call[0][0]
        assert "GAME IS OVER" in pending_call[0][2]  # setex(key, ttl, prompt)

    def test_close_with_inject_uses_director_prompt(self):
        """Director close=true + inject → uses Director's prompt, not default."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch
        from roast.types import Phase
        from roast.modes.roast_together import RoastTogetherMode

        redis = MagicMock()
        redis.setex = AsyncMock()
        state = self._state(turn_count=5)
        mode = RoastTogetherMode()

        with patch.object(mode, '_direct', new_callable=AsyncMock) as mock_direct:
            mock_direct.return_value = {
                "action": "inject", "best_take": "Great line!",
                "prompt": "Custom closing prompt.", "close": True,
            }
            result = asyncio.run(mode.tick(state, records=[], redis=redis))

        assert result == "Custom closing prompt."
        assert state.phase == Phase.CLOSING
        assert state.extra["best_take"] == "Great line!"
        # Verify the custom prompt was written, not the default
        pending_call = redis.setex.call_args_list[0]
        assert "pending_prompt" in pending_call[0][0]
        assert pending_call[0][2] == "Custom closing prompt."  # setex(key, ttl, prompt)

    def test_inject_without_close_no_phase_change(self):
        """Director inject without close → prompt written, phase stays ACTIVE."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch
        from roast.types import Phase
        from roast.modes.roast_together import RoastTogetherMode

        redis = MagicMock()
        redis.setex = AsyncMock()
        state = self._state(turn_count=2)
        mode = RoastTogetherMode()

        with patch.object(mode, '_direct', new_callable=AsyncMock) as mock_direct:
            mock_direct.return_value = {
                "action": "inject", "best_take": "Nice!",
                "prompt": "Keep going, dig deeper.", "close": False,
            }
            result = asyncio.run(mode.tick(state, records=[], redis=redis))

        assert result == "Keep going, dig deeper."
        assert state.phase == Phase.ACTIVE  # unchanged
        assert state.extra["best_take"] == "Nice!"


class TestWriteDirectorLog:
    """Tests for _write_director_log — fire-and-forget PG write for Director decisions."""

    def test_writes_row_via_pg_pool(self):
        import asyncio; from roast.base import _write_director_log
        from unittest.mock import AsyncMock, MagicMock, patch

        mock_conn = AsyncMock()
        mock_pool = MagicMock()
        mock_pool.acquire = MagicMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

        result = {"action": "inject", "best_take": "That's gold!", "prompt": "Amplify it.", "close": False}

        with patch("bootstrap.factory.get_pg_pool", new_callable=AsyncMock) as mock_get_pool:
            mock_get_pool.return_value = mock_pool
            asyncio.run(_write_director_log("rid-1", 5, result))

        mock_conn.execute.assert_called_once()
        args = mock_conn.execute.call_args[0][1:]
        assert args[0] == "rid-1"
        assert args[1] == 5
        assert args[2] == "inject"
        assert args[3] == "That's gold!"
        assert args[4] == "Amplify it."
        assert args[5] is False

    def test_handles_pg_failure_gracefully(self):
        import asyncio; from roast.base import _write_director_log
        from unittest.mock import AsyncMock, MagicMock, patch

        mock_pool = MagicMock()
        mock_pool.acquire = MagicMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(side_effect=Exception("PG down"))
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

        with patch("bootstrap.factory.get_pg_pool", new_callable=AsyncMock) as mock_get_pool:
            mock_get_pool.return_value = mock_pool
            # Should not raise
            asyncio.run(_write_director_log("rid-2", 3, {"action": "none"}))
