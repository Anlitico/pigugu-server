"""Tests for individual game modes  -  tick, state, triggers."""

from unittest.mock import MagicMock

from roast.state import RoastState
from roast.types import Mode, Phase


def _state(**kw):
    s = RoastState.__new__(RoastState)
    s.user_id = "u1"
    s.persona_id = 1
    s.roast_id = "n1"
    s.mode = kw.pop("mode", Mode.ROAST_TOGETHER)
    s.roast_instance_id = "test-id"
    s.phase = kw.pop("phase", Phase.ACTIVE)
    s.turn_count = kw.pop("turn_count", 0)
    s.extra = kw.pop("extra", {})
    return s


class _FakeTurn:
    def __init__(self, role, content):
        self.role = role
        self.content = content


# -------------------------------------------------------------------
# RoastTogetherMode
# -------------------------------------------------------------------

class TestRoastTogetherState:
    def test_init_extra(self):
        from roast.modes.roast_together import RoastTogetherMode
        extra = RoastTogetherMode.init_extra()
        assert extra == {"settled": False, "best_take": ""}


class TestRoastTogetherTriggers:
    def test_single_trigger_only(self):
        from roast.modes.roast_together import RoastTogetherMode
        mode = RoastTogetherMode()
        names = [t.name for t in mode.triggers]
        assert names == ["ending_max_turns"]

    def test_max_turns_is_8(self):
        from roast.modes.roast_together import RoastTogetherMode
        mode = RoastTogetherMode()
        assert mode.max_turns == 8

    def test_ending_fires_at_max_turns(self):
        from roast.modes.roast_together import RoastTogetherMode
        mode = RoastTogetherMode()
        state = _state(turn_count=8)
        trigger = [t for t in mode.triggers if t.name == "ending_max_turns"][0]
        assert trigger.check(state, [])

    def test_ending_not_fires_early(self):
        from roast.modes.roast_together import RoastTogetherMode
        mode = RoastTogetherMode()
        state = _state(turn_count=5)
        trigger = [t for t in mode.triggers if t.name == "ending_max_turns"][0]
        assert not trigger.check(state, [])

    def test_ending_affects_phase(self):
        from roast.modes.roast_together import RoastTogetherMode
        mode = RoastTogetherMode()
        trigger = [t for t in mode.triggers if t.name == "ending_max_turns"][0]
        assert trigger.affects_phase is True


# -------------------------------------------------------------------
# mark_roast_complete Tool
# -------------------------------------------------------------------

class TestMarkRoastComplete:
    def test_returns_false_no_active_user(self):
        import importlib
        from unittest.mock import MagicMock
        # Import create_roast_complete_tool directly, bypassing tools/__init__.py
        # which would trigger the OpenAI client init chain.
        mod = importlib.import_module("tools.roast")
        create_roast_complete_tool = mod.create_roast_complete_tool

        redis = MagicMock()
        tool = create_roast_complete_tool(redis=redis)
        import asyncio
        result = asyncio.run(tool.execute({}))
        assert result["settled"] is False
        assert result["reason"] == "no active user"

    def test_returns_false_no_active_roast(self):
        from unittest.mock import MagicMock, AsyncMock
        from tools.roast import create_roast_complete_tool, _current_user_id
        import contextvars

        redis = MagicMock()
        redis.get = AsyncMock(return_value=None)  # Redis returns no active roast

        token = _current_user_id.set("u1")
        try:
            tool = create_roast_complete_tool(redis=redis)
            import asyncio; result = asyncio.run(tool.execute({}))
            assert result["settled"] is False
            assert result["reason"] == "no active roast"
        finally:
            _current_user_id.reset(token)

    def test_returns_false_already_closed(self):
        from unittest.mock import MagicMock, AsyncMock
        from tools.roast import create_roast_complete_tool, _current_user_id
        from roast.types import Phase
        import json

        state_data = json.dumps({
            "roast_instance_id": "test-1",
            "user_id": "u1",
            "persona_id": 1,
            "roast_id": "n1",
            "mode": "roast_together",
            "phase": Phase.CLOSED,
            "turn_count": 5,
            "extra": {},
        })

        redis = MagicMock()
        redis.get = AsyncMock(return_value=state_data)

        token = _current_user_id.set("u1")
        try:
            tool = create_roast_complete_tool(redis=redis)
            import asyncio; result = asyncio.run(tool.execute({}))
            assert result["settled"] is False
            assert "already settled or closed" in result["reason"]
        finally:
            _current_user_id.reset(token)

    def test_settles_from_active(self):
        from unittest.mock import MagicMock, AsyncMock
        from tools.roast import create_roast_complete_tool, _current_user_id
        from roast.types import Phase
        import json

        state_data = json.dumps({
            "roast_instance_id": "test-1",
            "user_id": "u1",
            "persona_id": 1,
            "roast_id": "n1",
            "mode": "roast_together",
            "phase": Phase.ACTIVE,
            "turn_count": 3,
            "extra": {},
        })

        redis = MagicMock()
        redis.get = AsyncMock(return_value=state_data)
        redis.setex = AsyncMock()

        token = _current_user_id.set("u1")
        try:
            tool = create_roast_complete_tool(redis=redis)
            import asyncio; result = asyncio.run(tool.execute({}))
            assert result["settled"] is True
        finally:
            _current_user_id.reset(token)

    def test_settles_from_closing(self):
        from unittest.mock import MagicMock, AsyncMock
        from tools.roast import create_roast_complete_tool, _current_user_id
        from roast.types import Phase
        import json

        state_data = json.dumps({
            "roast_instance_id": "test-2",
            "user_id": "u1",
            "persona_id": 1,
            "roast_id": "n2",
            "mode": "roast_together",
            "phase": Phase.CLOSING,
            "turn_count": 6,
            "extra": {},
        })

        redis = MagicMock()
        redis.get = AsyncMock(return_value=state_data)
        redis.setex = AsyncMock()

        token = _current_user_id.set("u1")
        try:
            tool = create_roast_complete_tool(redis=redis)
            import asyncio; result = asyncio.run(tool.execute({}))
            assert result["settled"] is True
        finally:
            _current_user_id.reset(token)


# -------------------------------------------------------------------
# DebateBickerMode
# -------------------------------------------------------------------

class TestDebateBickerState:
    def test_init_extra(self):
        from roast.modes.debate_bicker import DebateBickerMode
        extra = DebateBickerMode.init_extra()
        assert extra["strong_points"] == 0
        assert extra["fart_type"] == ""
        assert extra["debate_history"] == []

    def test_is_strong_point_detects_data(self):
        from roast.modes.debate_bicker import _is_strong_point
        assert _is_strong_point("According to the data, a recent study shows a 15% increase in evidence-based arguments.")
        assert not _is_strong_point("I think you're wrong.")
        assert not _is_strong_point("short")

    def test_update_state_increments_strong(self):
        from roast.modes.debate_bicker import DebateBickerMode
        mode = DebateBickerMode()
        state = _state(turn_count=1, mode=Mode.DEBATE_BICKER,
                       extra={"strong_points": 0, "fart_type": "", "debate_history": []})
        records = [_FakeTurn("user", "According to the latest research, this policy has a 25% approval rating based on data from Pew.")]
        mode._update_state(state, records)
        assert state.extra["strong_points"] == 1

    def test_update_state_skips_weak(self):
        from roast.modes.debate_bicker import DebateBickerMode
        mode = DebateBickerMode()
        state = _state(turn_count=1, mode=Mode.DEBATE_BICKER,
                       extra={"strong_points": 0})
        records = [_FakeTurn("user", "nah")]
        mode._update_state(state, records)
        assert state.extra["strong_points"] == 0


class TestDebateBickerTriggers:
    def test_all_triggers_registered(self):
        from roast.modes.debate_bicker import DebateBickerMode
        mode = DebateBickerMode()
        names = [t.name for t in mode.triggers]
        assert "user_won" in names
        assert "ending_max_turns" in names
        assert "user_repeat" in names

    def test_user_won_fires(self):
        from roast.modes.debate_bicker import DebateBickerMode
        mode = DebateBickerMode()
        state = _state(turn_count=4, mode=Mode.DEBATE_BICKER,
                       extra={"strong_points": 3})
        trigger = [t for t in mode.triggers if t.name == "user_won"][0]
        assert trigger.check(state, [])

    def test_repeat_detection(self):
        from roast.modes.debate_bicker import _detect_repeat
        records = [_FakeTurn("user", "I disagree"), _FakeTurn("user", "I disagree")]
        assert _detect_repeat(records)

        records2 = [_FakeTurn("user", "I disagree"), _FakeTurn("user", "Actually you're right")]
        assert not _detect_repeat(records2)


# -------------------------------------------------------------------
# BreakingBombMode
# -------------------------------------------------------------------

class TestBreakingBombState:
    def test_init_extra(self):
        from roast.modes.breaking_bomb import BreakingBombMode
        extra = BreakingBombMode.init_extra()
        assert extra == {"reactions": [], "best_take": ""}

    def test_update_state_records_reaction(self):
        from roast.modes.breaking_bomb import BreakingBombMode
        mode = BreakingBombMode()
        state = _state(turn_count=1, mode=Mode.BREAKING_BOMB,
                       extra={"reactions": []})
        records = [_FakeTurn("user", "Wow this is HUGE!!!")]
        mode._update_state(state, records)
        assert len(state.extra["reactions"]) == 1
        assert state.extra["reactions"][0]["turn"] == 1
        assert "HUGE" in state.extra["reactions"][0]["text"]

    def test_single_trigger(self):
        from roast.modes.breaking_bomb import BreakingBombMode
        mode = BreakingBombMode()
        triggers = mode.triggers
        assert len(triggers) == 1
        assert triggers[0].name == "ending_max_turns"
