"""Tests for individual game modes — tick, state, triggers."""

from unittest.mock import MagicMock

from roast.state import RoastState
from roast.types import Mode, Phase


def _state(**kw):
    s = RoastState.__new__(RoastState)
    s.user_id = "u1"
    s.persona_id = "trump"
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


# ═══════════════════════════════════════════════════════════════════
# RoastTogetherMode
# ═══════════════════════════════════════════════════════════════════

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


class TestRoastTogetherTriggers:
    def test_all_triggers_registered(self):
        from roast.modes.roast_together import RoastTogetherMode
        mode = RoastTogetherMode()
        names = [t.name for t in mode.triggers]
        assert "ending_max_turns" in names
        assert "user_spicy" in names
        assert "user_disengaged" in names

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


# ═══════════════════════════════════════════════════════════════════
# DebateBickerMode
# ═══════════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════════
# BreakingBombMode
# ═══════════════════════════════════════════════════════════════════

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
