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

class TestRoastTogetherEnergy:
    def test_empty(self):
        from roast.modes.roast_together import _compute_energy
        assert _compute_energy("") == 0.0

    def test_low_energy(self):
        from roast.modes.roast_together import _compute_energy
        e = _compute_energy("yeah whatever")
        assert e < 0.3

    def test_high_energy(self):
        from roast.modes.roast_together import _compute_energy
        e = _compute_energy("This is ABSOLUTELY INSANE!!! I cannot believe this ridiculous take on the situation!!!")
        assert e > 0.5

    def test_spicy_words(self):
        from roast.modes.roast_together import _compute_energy
        e = _compute_energy("absolutely totally completely insane ridiculous outrageous")
        assert e >= 0.2  # spicy words contribute


class TestRoastTogetherState:
    def test_init_extra(self):
        from roast.modes.roast_together import RoastTogetherMode
        extra = RoastTogetherMode.init_extra()
        assert extra["user_energy"] == 0.0
        assert extra["best_take"] == ""
        assert extra["best_take_energy"] == 0.0
        assert extra["has_best_take"] is False
        assert extra["score_breakdown"] == {}
        assert extra["settled"] is False

    def test_update_state_tracks_energy(self):
        from roast.modes.roast_together import RoastTogetherMode
        mode = RoastTogetherMode()
        state = _state(turn_count=1, extra={"user_energy": 0.0, "best_take": "", "best_take_energy": 0.0, "best_take_turn": 0})
        records = [_FakeTurn("user", "This is TOTALLY ridiculous!!! I can't even.")]
        mode._update_state(state, records)
        assert state.extra["user_energy"] > 0.3

    def test_update_state_captures_best_take(self):
        from roast.modes.roast_together import RoastTogetherMode
        mode = RoastTogetherMode()
        state = _state(turn_count=1, extra={"user_energy": 0.0, "best_take": "", "best_take_energy": 0.0, "best_take_turn": 0})
        records = [_FakeTurn("user", "This is the most ridiculous thing I have ever seen in my entire life!! Absolutely insane!!!")]
        mode._update_state(state, records)
        assert state.extra["best_take"] != ""
        assert state.extra["best_take_energy"] > 0.5

    def test_skips_weak_take(self):
        from roast.modes.roast_together import RoastTogetherMode
        mode = RoastTogetherMode()
        state = _state(turn_count=1)
        state.extra = {"user_energy": 0.0, "best_take": "", "best_take_energy": 0.0, "best_take_turn": 0}
        records = [_FakeTurn("user", "yeah")]
        mode._update_state(state, records)
        assert state.extra["best_take"] == ""  # too short, low energy


class TestRoastTogetherSaturated:
    def test_not_enough_turns(self):
        from roast.modes.roast_together import _saturated
        state = _state(turn_count=2)
        records = [_FakeTurn("user", "ok"), _FakeTurn("user", "yeah")]
        assert not _saturated(state, records)

    def test_not_enough_user_msgs(self):
        from roast.modes.roast_together import _saturated
        state = _state(turn_count=3)
        records = [_FakeTurn("assistant", "long reply"), _FakeTurn("user", "ok")]
        assert not _saturated(state, records)

    def test_high_energy_not_saturated(self):
        from roast.modes.roast_together import _saturated
        state = _state(turn_count=3)
        records = [
            _FakeTurn("assistant", "..."),
            _FakeTurn("user", "This is ABSOLUTELY RIDICULOUS!!! I cannot believe this!!"),
            _FakeTurn("user", "And another thing — this is totally insane!!"),
        ]
        assert not _saturated(state, records)

    def test_low_energy_saturated(self):
        from roast.modes.roast_together import _saturated
        state = _state(turn_count=3)
        records = [
            _FakeTurn("assistant", "..."),
            _FakeTurn("user", "yeah ok"),
            _FakeTurn("user", "i guess so"),
            _FakeTurn("user", "whatever"),
        ]
        assert _saturated(state, records)


class TestRoastTogetherTriggers:
    def test_all_triggers_registered(self):
        from roast.modes.roast_together import RoastTogetherMode
        mode = RoastTogetherMode()
        names = [t.name for t in mode.triggers]
        assert "roast_saturated" in names
        assert "ending_max_turns" in names
        assert "user_spicy" in names
        assert "user_disengaged" in names

    def test_max_turns_is_8(self):
        from roast.modes.roast_together import RoastTogetherMode
        mode = RoastTogetherMode()
        assert mode.max_turns == 8

    def test_saturated_affects_phase(self):
        from roast.modes.roast_together import RoastTogetherMode
        mode = RoastTogetherMode()
        trigger = [t for t in mode.triggers if t.name == "roast_saturated"][0]
        assert trigger.affects_phase is True

    def test_saturated_fires(self):
        from roast.modes.roast_together import RoastTogetherMode
        mode = RoastTogetherMode()
        state = _state(turn_count=3)
        records = [
            _FakeTurn("user", "yeah"),
            _FakeTurn("user", "ok"),
            _FakeTurn("user", "whatever"),
        ]
        trigger = [t for t in mode.triggers if t.name == "roast_saturated"][0]
        assert trigger.check(state, records)

    def test_spicy_fires(self):
        from roast.modes.roast_together import RoastTogetherMode
        mode = RoastTogetherMode()
        state = _state(turn_count=2, extra={"user_energy": 0.9})
        trigger = [t for t in mode.triggers if t.name == "user_spicy"][0]
        assert trigger.check(state, [])

    def test_disengaged_fires(self):
        from roast.modes.roast_together import RoastTogetherMode
        mode = RoastTogetherMode()
        state = _state(turn_count=3)
        records = [_FakeTurn("user", "ok"), _FakeTurn("user", "no"), _FakeTurn("user", "yeah")]
        trigger = [t for t in mode.triggers if t.name == "user_disengaged"][0]
        assert trigger.check(state, records)


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
        import json, contextvars

        state_data = json.dumps({
            "roast_instance_id": "test-1",
            "user_id": "u1",
            "persona_id": 1,
            "roast_id": "n1",
            "mode": "roast_together",
            "phase": Phase.CLOSED,
            "turn_count": 5,
            "extra": {"best_take_energy": 0.8},
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
        import json, contextvars

        state_data = json.dumps({
            "roast_instance_id": "test-1",
            "user_id": "u1",
            "persona_id": 1,
            "roast_id": "n1",
            "mode": "roast_together",
            "phase": Phase.ACTIVE,
            "turn_count": 3,
            "extra": {"best_take_energy": 0.85, "best_take": "great roast"},
        })

        redis = MagicMock()
        redis.get = AsyncMock(return_value=state_data)
        redis.setex = AsyncMock()

        token = _current_user_id.set("u1")
        try:
            tool = create_roast_complete_tool(redis=redis)
            import asyncio; result = asyncio.run(tool.execute({}))
            assert result["settled"] is True
            assert result["has_best_take"] is True  # 0.85 > 0.70
        finally:
            _current_user_id.reset(token)

    def test_has_best_take_false_below_threshold(self):
        from unittest.mock import MagicMock, AsyncMock
        from tools.roast import create_roast_complete_tool, _current_user_id
        from roast.types import Phase
        import json, contextvars

        state_data = json.dumps({
            "roast_instance_id": "test-2",
            "user_id": "u1",
            "persona_id": 1,
            "roast_id": "n2",
            "mode": "roast_together",
            "phase": Phase.CLOSING,
            "turn_count": 6,
            "extra": {"best_take_energy": 0.55, "best_take": "ok roast"},
        })

        redis = MagicMock()
        redis.get = AsyncMock(return_value=state_data)
        redis.setex = AsyncMock()

        token = _current_user_id.set("u1")
        try:
            tool = create_roast_complete_tool(redis=redis)
            import asyncio; result = asyncio.run(tool.execute({}))
            assert result["settled"] is True
            assert result["has_best_take"] is False  # 0.55 < 0.70
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
        assert extra == {"reactions": []}

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
