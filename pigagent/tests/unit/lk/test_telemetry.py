# tests/unit/lk/test_telemetry.py
"""Unit tests for TelemetryCollector singleton."""

import pytest
from metrics.turn import TelemetryCollector, _diff, _fmt


@pytest.fixture(autouse=True)
def _reset_collector():
    """Ensure clean state before every test."""
    import metrics.turn as mod
    mod._current = None
    yield
    mod._current = None


class TestTelemetryCollector:
    def test_start_turn_creates_turn(self):
        TelemetryCollector.start_turn(user_id="u1", persona_id=1)
        TelemetryCollector.mark("vad_start")
        # Should not raise and should produce a METRIC on finish
        # (won't log because missing llm_start, but shouldn't crash)

    def test_mark_does_nothing_when_no_turn(self):
        import metrics.turn as mod
        mod._current = None
        TelemetryCollector.mark("vad_start")  # no-op, no error

    def test_finish_turn_does_nothing_when_no_turn(self):
        import metrics.turn as mod
        mod._current = None
        TelemetryCollector.finish_turn()  # no-op, no error

    def test_start_turn_flushes_previous(self):
        """When a turn has llm_start, starting a new turn flushes it."""
        import metrics.turn as mod

        TelemetryCollector.start_turn(user_id="u1", persona_id=1)
        TelemetryCollector.mark("vad_start")
        TelemetryCollector.mark("llm_start")
        TelemetryCollector.mark("agent_spk")
        old_turn = mod._current

        # Starting a new turn should flush the previous one
        TelemetryCollector.start_turn(user_id="u2", persona_id=1)
        new_turn = mod._current

        # New turn is active, old turn was flushed
        assert new_turn is not old_turn
        assert new_turn is not None
        assert new_turn["user_id"] == "u2"
        assert mod._current is not None

    def test_set_meta_turn_number(self):
        """set_meta with turn_number aligns the turn_id."""
        import metrics.turn as mod

        TelemetryCollector.start_turn(user_id="u1", persona_id=1)
        TelemetryCollector.mark("llm_start")  # ensure turn is "complete"
        TelemetryCollector.set_meta("llm_model", "qwen-plus")
        TelemetryCollector.set_meta("turn_number", 125)

        assert mod._current is not None
        assert mod._current["meta"]["llm_model"] == "qwen-plus"
        assert mod._current["meta"]["turn_number"] == 125
        assert mod._current["turn_id"] == 125

    def test_incomplete_turn_not_logged(self):
        """Turns without llm_start are skipped in _log()."""
        import metrics.turn as mod

        TelemetryCollector.start_turn(user_id="u1", persona_id=1)
        TelemetryCollector.mark("vad_start")
        TelemetryCollector.mark("vad_end")

        # finish_turn calls _log which skips when llm_start is missing
        TelemetryCollector.finish_turn()
        # _current should be cleared even for incomplete turns
        assert mod._current is None

    def test_has_mark_true(self):
        TelemetryCollector.start_turn(user_id="u1", persona_id=1)
        TelemetryCollector.mark("vad_start")
        assert TelemetryCollector.has_mark("vad_start") is True
        TelemetryCollector.finish_turn()

    def test_has_mark_false(self):
        TelemetryCollector.start_turn(user_id="u1", persona_id=1)
        assert TelemetryCollector.has_mark("vad_end") is False
        TelemetryCollector.finish_turn()

    def test_has_mark_no_turn(self):
        import metrics.turn as mod
        mod._current = None
        assert TelemetryCollector.has_mark("vad_end") is False

    def test_vad_end_not_overwritten(self):
        """Simulate agent speech gap: vad_end is only recorded once."""
        import metrics.turn as mod
        import time

        TelemetryCollector.start_turn(user_id="u1", persona_id=1)
        TelemetryCollector.mark("vad_start")
        TelemetryCollector.mark("vad_end")
        time.sleep(0.01)
        assert mod._current is not None
        first = mod._current["marks"]["vad_end"]
        time.sleep(0.01)
        if not TelemetryCollector.has_mark("vad_end"):
            TelemetryCollector.mark("vad_end")
        second = mod._current["marks"].get("vad_end")
        assert second == first
        TelemetryCollector.finish_turn()


class TestDiff:
    def test_positive(self):
        assert _diff({"a": 1.0, "b": 2.5}, "a", "b") == 1.5

    def test_missing_key(self):
        assert _diff({"a": 1.0}, "a", "b") is None
        assert _diff({"b": 2.0}, "a", "b") is None

    def test_empty_marks(self):
        assert _diff({}, "a", "b") is None


class TestFmt:
    def test_value(self):
        assert _fmt(1.234) == "1.234s"

    def test_none(self):
        assert _fmt(None) == "—"
