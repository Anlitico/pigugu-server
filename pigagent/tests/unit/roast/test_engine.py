"""Tests for consume and GameMode.tick."""

from unittest.mock import MagicMock

from roast.pending import consume
from roast.state import RoastState
from model import Mode, Phase
from roast.modes.roast_together import RoastTogetherMode


def _state(**kw):
    s = RoastState.__new__(RoastState)
    s.user_id = "u1"
    s.persona_id = "trump"
    s.news_id = "n1"
    s.mode = kw.pop("mode", Mode.ROAST_TOGETHER)
    s.roast_id = "test-id"
    s.phase = kw.pop("phase", Phase.ACTIVE)
    s.turn_count = kw.pop("turn_count", 0)
    s.extra = kw.pop("extra", {})
    return s


class TestConsumePendingPrompt:
    def test_none(self):
        redis = MagicMock()
        redis.get.return_value = None
        assert consume("r1", redis) is None

    def test_exists(self):
        redis = MagicMock()
        redis.get.return_value = b"test prompt"
        result = consume("r1", redis)
        assert result == "test prompt"
        redis.delete.assert_called_once()


class TestGameModeTick:
    def test_ending_on_max_turns(self):
        redis = MagicMock()
        state = _state(turn_count=4)
        mode = RoastTogetherMode()

        import asyncio
        result = asyncio.run(mode.tick(state, records=[], redis=redis))
        assert result is not None
        assert state.turn_count == 5

    def test_no_transition_mid_game(self):
        redis = MagicMock()
        state = _state(turn_count=1)
        mode = RoastTogetherMode()

        import asyncio
        result = asyncio.run(mode.tick(state, records=[], redis=redis))
        assert result is None
        assert state.turn_count == 2

    def test_skips_if_not_active(self):
        redis = MagicMock()
        state = _state(phase=Phase.REVIEW, turn_count=5)
        mode = RoastTogetherMode()

        import asyncio
        result = asyncio.run(mode.tick(state, records=[], redis=redis))
        assert result is None
        assert state.turn_count == 5
