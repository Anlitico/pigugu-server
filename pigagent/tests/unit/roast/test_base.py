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
