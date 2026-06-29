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
    s.started_at = kw.pop("started_at", 0.0)
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
        assert extra["settled"] is False
        assert extra["best_take"] == ""
        assert extra["scores"] == []


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
        assert extra["best_take"] == ""
        assert extra["support_history"] == []

    def test_max_turns_is_8(self):
        from roast.modes.debate_bicker import DebateBickerMode
        mode = DebateBickerMode()
        assert mode.max_turns == 8

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
# Director Schema
# -------------------------------------------------------------------

class TestRoastTogetherDirectorSchema:
    def test_schema_has_all_fields(self):
        from roast.modes.roast_together import RoastTogetherMode
        mode = RoastTogetherMode()
        schema = mode.get_director_schema()
        props = schema["schema"]["properties"]
        required = schema["schema"]["required"]
        # Base fields
        assert "action" in props
        assert "best_take" in props
        assert "prompt" in props
        assert "close" in props
        # New scoring fields
        assert "score" in props
        assert "rating" in props
        assert "highlighted_quote" in props
        assert "score" in required
        assert "rating" in required
        assert "highlighted_quote" in required
        assert props["score"]["minimum"] == 1
        assert props["score"]["maximum"] == 12
        assert props["rating"]["enum"] == ["meh", "decent", "spicy", "fire", "superb", "godlike"]

    def test_schema_strict(self):
        from roast.modes.roast_together import RoastTogetherMode
        mode = RoastTogetherMode()
        schema = mode.get_director_schema()
        assert schema["strict"] is True
        assert schema["name"] == "director_output"


class TestDebateBickerDirectorSchema:
    def test_schema_has_all_fields(self):
        from roast.modes.debate_bicker import DebateBickerMode
        mode = DebateBickerMode()
        schema = mode.get_director_schema()
        props = schema["schema"]["properties"]
        required = schema["schema"]["required"]
        # Base fields
        assert "action" in props
        assert "best_take" in props
        assert "prompt" in props
        assert "close" in props
        # New polling fields
        assert "user_support" in props
        assert "opponent_support" in props
        assert "shift" in props
        assert "judge_comment" in props
        assert "user_support" in required
        assert "opponent_support" in required
        assert "shift" in required
        assert "judge_comment" in required
        assert props["user_support"]["minimum"] == 0.0
        assert props["user_support"]["maximum"] == 100.0

    def test_schema_strict(self):
        from roast.modes.debate_bicker import DebateBickerMode
        mode = DebateBickerMode()
        schema = mode.get_director_schema()
        assert schema["strict"] is True


# -------------------------------------------------------------------
# Real-time Push (_on_director_result)
# -------------------------------------------------------------------

class TestRoastTogetherDirectorPush:
    def _state(self, **kw):
        s = RoastState.__new__(RoastState)
        s.user_id = "u1"
        s.persona_id = 1
        s.roast_id = "n1"
        s.mode = Mode.ROAST_TOGETHER
        s.roast_instance_id = "rid-1"
        s.phase = Phase.ACTIVE
        s.turn_count = kw.pop("turn_count", 3)
        s.started_at = 0.0
        s.extra = kw.pop("extra", {"scores": []})
        return s

    def test_publishes_roast_score(self):
        import asyncio, json
        from unittest.mock import AsyncMock, MagicMock
        from roast.modes.roast_together import RoastTogetherMode

        redis = MagicMock()
        redis.publish = AsyncMock()
        mode = RoastTogetherMode()
        state = self._state(turn_count=3)
        director_result = {
            "score": 9, "rating": "fire",
            "highlighted_quote": "Bezos has no soul",
            "prompt": None,
        }

        asyncio.run(mode._on_director_result(state, director_result, redis))
        redis.publish.assert_called_once()
        args = redis.publish.call_args[0]
        assert args[0] == "ws:user:u1"
        event = json.loads(args[1])
        assert event["type"] == "roast_score"
        assert event["score"] == 9
        assert event["rating"] == "fire"
        assert event["highlighted_quote"] == "Bezos has no soul"
        assert event["round"] == 3
        assert event["roast_instance_id"] == "rid-1"

    def test_null_quote_below_fire(self):
        import asyncio, json
        from unittest.mock import AsyncMock, MagicMock
        from roast.modes.roast_together import RoastTogetherMode

        redis = MagicMock()
        redis.publish = AsyncMock()
        mode = RoastTogetherMode()
        state = self._state()
        director_result = {"score": 5, "rating": "decent", "highlighted_quote": None}

        asyncio.run(mode._on_director_result(state, director_result, redis))
        event = json.loads(redis.publish.call_args[0][1])
        assert event["highlighted_quote"] is None

    def test_accumulates_scores_in_state(self):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock
        from roast.modes.roast_together import RoastTogetherMode

        redis = MagicMock()
        redis.publish = AsyncMock()
        mode = RoastTogetherMode()
        state = self._state()

        director_result = {"score": 8, "rating": "spicy", "highlighted_quote": None}
        asyncio.run(mode._on_director_result(state, director_result, redis))
        assert len(state.extra["scores"]) == 1
        assert state.extra["scores"][0]["score"] == 8

        director_result = {"score": 11, "rating": "superb", "highlighted_quote": "Genius!"}
        asyncio.run(mode._on_director_result(state, director_result, redis))
        assert len(state.extra["scores"]) == 2
        assert state.extra["scores"][1]["rating"] == "superb"

    def test_no_redis_no_push(self):
        import asyncio
        from roast.modes.roast_together import RoastTogetherMode

        mode = RoastTogetherMode()
        state = self._state()
        # Should not raise
        asyncio.run(mode._on_director_result(state, {"score": 5, "rating": "decent"}, None))


class TestDebateBickerDirectorPush:
    def _state(self, **kw):
        s = RoastState.__new__(RoastState)
        s.user_id = "u1"
        s.roast_id = "d1"
        s.mode = Mode.DEBATE_BICKER
        s.roast_instance_id = "rid-2"
        s.phase = Phase.ACTIVE
        s.turn_count = kw.pop("turn_count", 2)
        s.started_at = 0.0
        s.extra = kw.pop("extra", {"support_history": []})
        return s

    def test_publishes_debate_judge(self):
        import asyncio, json
        from unittest.mock import AsyncMock, MagicMock
        from roast.modes.debate_bicker import DebateBickerMode

        redis = MagicMock()
        redis.publish = AsyncMock()
        mode = DebateBickerMode()
        state = self._state(turn_count=2)
        director_result = {
            "user_support": 55.0, "opponent_support": 45.0,
            "shift": 5.0, "judge_comment": "用户引用了数据",
            "prompt": None,
        }

        asyncio.run(mode._on_director_result(state, director_result, redis))
        redis.publish.assert_called_once()
        event = json.loads(redis.publish.call_args[0][1])
        assert event["type"] == "debate_judge"
        assert event["user_support"] == 55.0
        assert event["opponent_support"] == 45.0
        assert event["shift"] == 5.0
        assert event["judge_comment"] == "用户引用了数据"
        assert event["round"] == 2

    def test_accumulates_support_history(self):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock
        from roast.modes.debate_bicker import DebateBickerMode

        redis = MagicMock()
        redis.publish = AsyncMock()
        mode = DebateBickerMode()
        state = self._state()

        asyncio.run(mode._on_director_result(state, {
            "user_support": 52.0, "opponent_support": 48.0, "shift": 2.0, "judge_comment": "ok"
        }, redis))
        assert len(state.extra["support_history"]) == 1
        assert state.extra["support_history"][0]["user"] == 52.0

    def test_ko_triggers_at_75(self):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch
        from roast.modes.debate_bicker import DebateBickerMode
        from roast.types import Phase

        redis = MagicMock()
        redis.publish = AsyncMock()
        mode = DebateBickerMode()
        state = self._state(turn_count=5)

        with patch("roast.pending.write", new_callable=AsyncMock) as mock_pending:
            asyncio.run(mode._on_director_result(state, {
                "user_support": 78.0, "opponent_support": 22.0, "shift": 8.0, "judge_comment": "KO"
            }, redis))
            assert state.phase == Phase.CLOSING
            assert state.extra["ko"] is True
            mock_pending.assert_called_once()

    def test_ko_triggers_at_25(self):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch
        from roast.modes.debate_bicker import DebateBickerMode
        from roast.types import Phase

        redis = MagicMock()
        redis.publish = AsyncMock()
        mode = DebateBickerMode()
        state = self._state(turn_count=4)

        with patch("roast.pending.write", new_callable=AsyncMock) as mock_pending:
            asyncio.run(mode._on_director_result(state, {
                "user_support": 20.0, "opponent_support": 80.0, "shift": -10.0, "judge_comment": "lost"
            }, redis))
            assert state.phase == Phase.CLOSING
            assert state.extra["ko"] is True

    def test_no_ko_in_middle(self):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock
        from roast.modes.debate_bicker import DebateBickerMode
        from roast.types import Phase

        redis = MagicMock()
        redis.publish = AsyncMock()
        mode = DebateBickerMode()
        state = self._state()

        asyncio.run(mode._on_director_result(state, {
            "user_support": 55.0, "opponent_support": 45.0, "shift": 5.0, "judge_comment": "fine"
        }, redis))
        assert state.phase == Phase.ACTIVE  # unchanged
        assert state.extra.get("ko") is None


# -------------------------------------------------------------------
# Score computation
# -------------------------------------------------------------------

class TestRoastTogetherScore:
    def _state(self, **kw):
        s = RoastState.__new__(RoastState)
        s.user_id = "u1"
        s.roast_id = "n1"
        s.mode = Mode.ROAST_TOGETHER
        s.roast_instance_id = "rid-1"
        s.turn_count = kw.pop("turn_count", 4)
        s.phase = Phase.ACTIVE
        s.started_at = 0.0
        s.extra = kw.pop("extra", {})
        return s

    def test_score_aggregates(self):
        from roast.modes.roast_together import RoastTogetherMode
        mode = RoastTogetherMode()
        state = self._state(extra={
            "scores": [
                {"round": 1, "score": 3, "rating": "meh", "quote": None},
                {"round": 2, "score": 7, "rating": "spicy", "quote": None},
                {"round": 3, "score": 9, "rating": "fire", "quote": "Great line!"},
                {"round": 4, "score": 5, "rating": "decent", "quote": None},
            ]
        })
        result = mode.score(state)
        assert result["total_score"] == 24
        assert result["avg_score"] == 6.0
        assert result["best_rating"] == "fire"
        assert result["best_quote"] == "Great line!"
        assert result["turns"] == 4

    def test_score_empty(self):
        from roast.modes.roast_together import RoastTogetherMode
        mode = RoastTogetherMode()
        state = self._state(extra={"scores": []})
        result = mode.score(state)
        assert result["total_score"] == 0
        assert result["avg_score"] == 0.0
        assert result["best_rating"] == "meh"
        assert result["best_quote"] == ""


class TestDebateBickerScore:
    def _state(self, **kw):
        s = RoastState.__new__(RoastState)
        s.user_id = "u1"
        s.roast_id = "d1"
        s.mode = Mode.DEBATE_BICKER
        s.roast_instance_id = "rid-2"
        s.turn_count = kw.pop("turn_count", 3)
        s.phase = Phase.ACTIVE
        s.started_at = 0.0
        s.extra = kw.pop("extra", {})
        return s

    def test_landslide_win(self):
        from roast.modes.debate_bicker import DebateBickerMode
        mode = DebateBickerMode()
        state = self._state(extra={
            "strong_points": 3,
            "support_history": [{"round": 3, "user": 80.0, "opponent": 20.0, "shift": 10.0}],
        })
        result = mode.score(state)
        assert result["final_user_support"] == 80.0
        assert result["debate_result"] == "landslide_win"

    def test_narrow_win(self):
        from roast.modes.debate_bicker import DebateBickerMode
        mode = DebateBickerMode()
        state = self._state(extra={
            "strong_points": 2,
            "support_history": [{"round": 3, "user": 62.0, "opponent": 38.0, "shift": 5.0}],
        })
        result = mode.score(state)
        assert result["debate_result"] == "narrow_win"

    def test_draw(self):
        from roast.modes.debate_bicker import DebateBickerMode
        mode = DebateBickerMode()
        state = self._state(extra={
            "strong_points": 1,
            "support_history": [{"round": 3, "user": 50.0, "opponent": 50.0, "shift": 0.0}],
        })
        result = mode.score(state)
        assert result["debate_result"] == "draw"

    def test_narrow_loss(self):
        from roast.modes.debate_bicker import DebateBickerMode
        mode = DebateBickerMode()
        state = self._state(extra={
            "strong_points": 0,
            "support_history": [{"round": 3, "user": 35.0, "opponent": 65.0, "shift": -5.0}],
        })
        result = mode.score(state)
        assert result["debate_result"] == "narrow_loss"

    def test_landslide_loss(self):
        from roast.modes.debate_bicker import DebateBickerMode
        mode = DebateBickerMode()
        state = self._state(extra={
            "strong_points": 0,
            "support_history": [{"round": 3, "user": 18.0, "opponent": 82.0, "shift": -12.0}],
        })
        result = mode.score(state)
        assert result["debate_result"] == "landslide_loss"

    def test_default_50_when_no_history(self):
        from roast.modes.debate_bicker import DebateBickerMode
        mode = DebateBickerMode()
        state = self._state(extra={"strong_points": 0, "support_history": []})
        result = mode.score(state)
        assert result["final_user_support"] == 50.0

    def test_debate_result_helper(self):
        from roast.modes.debate_bicker import _debate_result
        assert _debate_result(80.0) == "landslide_win"
        assert _debate_result(60.0) == "narrow_win"
        assert _debate_result(50.0) == "draw"
        assert _debate_result(30.0) == "narrow_loss"
        assert _debate_result(15.0) == "landslide_loss"
