"""Tests for RoastState."""

from roast.state import RoastState
from model import Mode, Phase


def _make(**kw):
    """Construct a RoastState for testing (bypasses Redis)."""
    state = RoastState.__new__(RoastState)
    state.user_id = kw.get("user_id", "u1")
    state.persona_id = kw.get("persona_id", "trump")
    state.news_id = kw.get("news_id", "n1")
    state.mode = kw.get("mode", Mode.ROAST_TOGETHER)
    state.roast_id = kw.get("roast_id", "test-id")
    state.phase = kw.get("phase", Phase.ACTIVE)
    state.turn_count = kw.get("turn_count", 0)
    state.extra = kw.get("extra", {})
    return state


class TestRoastState:
    def test_fresh_defaults(self):
        s = _make()
        assert s.phase == Phase.ACTIVE
        assert s.turn_count == 0
        assert s.extra == {}

    def test_to_dict(self):
        s = _make()
        d = s.to_dict()
        assert d["roast_id"] == "test-id"
        assert d["mode"] == "roast_together"

    def test_from_dict_roundtrip(self):
        s = _make(turn_count=3, extra={"score": 10})
        s2 = RoastState.from_dict(s.to_dict())
        assert s2.turn_count == 3
        assert s2.extra == {"score": 10}

    def test_mutation(self):
        s = _make()
        s.turn_count = 3
        s.extra["key"] = "val"
        assert s.turn_count == 3
        assert s.extra["key"] == "val"
