# tests/unit/lk/test_telemetry.py
"""Unit tests for TurnTimer."""

from lk.telemetry import TurnTimer


class TestTurnTimer:
    def test_initial_state(self):
        timer = TurnTimer()
        assert timer.data["turn_id"] == 0
        assert timer.data["user_stop_speaking"] is None
        assert timer.data["agent_start_speaking"] is None

    def test_mark_sets_timestamp(self):
        timer = TurnTimer()
        timer.mark("user_stop_speaking")
        assert timer.data["user_stop_speaking"] is not None

    def test_reset_increments_turn_and_clears_timestamps(self):
        timer = TurnTimer()
        timer.mark("user_stop_speaking")
        timer.mark("agent_start_thinking")
        timer.mark("agent_start_speaking")

        timer.reset()

        assert timer.data["turn_id"] == 1
        assert timer.data["user_stop_speaking"] is None
        assert timer.data["agent_start_thinking"] is None
        # agent_start_speaking is not cleared by reset
        assert timer.data["agent_start_speaking"] is None

    def test_log_summary_no_t5_does_nothing(self):
        timer = TurnTimer()
        # No agent_start_speaking set → log_summary should return early
        timer.mark("user_stop_speaking")
        timer.mark("agent_start_thinking")
        # Should not raise
        timer.log_summary()

    def test_multiple_turns_increment(self):
        timer = TurnTimer()
        timer.reset()
        assert timer.data["turn_id"] == 1
        timer.reset()
        assert timer.data["turn_id"] == 2
        timer.reset()
        assert timer.data["turn_id"] == 3
