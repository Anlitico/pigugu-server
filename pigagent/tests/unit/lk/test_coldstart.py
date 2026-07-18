# tests/unit/lk/test_coldstart.py
"""Unit tests for ColdStartMetrics singleton."""

import pytest
from metrics.session import ColdStartMetrics, _diff, _fmt


@pytest.fixture(autouse=True)
def _reset_collector():
    """Ensure clean state before every test."""
    import metrics.session as mod
    mod._session = None
    yield
    mod._session = None


class TestColdStartMetrics:
    def test_start_creates_session(self):
        ColdStartMetrics.start(session_id="job-001", room_name="test-room")
        ColdStartMetrics.mark("stt_init")
        ColdStartMetrics.mark("tts_init")
        ColdStartMetrics.mark("vad")
        ColdStartMetrics.mark("ready")
        # Should not crash

    def test_mark_does_nothing_when_no_session(self):
        import metrics.session as mod
        mod._session = None
        ColdStartMetrics.mark("entry")  # no-op, no error

    def test_flush_does_nothing_when_no_session(self):
        import metrics.session as mod
        mod._session = None
        ColdStartMetrics.flush()  # no-op, no error

    def test_start_flushes_previous(self):
        """When a session has 'ready', starting a new one flushes it."""
        import metrics.session as mod

        ColdStartMetrics.start(session_id="job-001", room_name="r1")
        ColdStartMetrics.mark("stt_init")
        ColdStartMetrics.mark("ready")
        old_session = mod._session

        ColdStartMetrics.start(session_id="job-002", room_name="r2")
        new_session = mod._session

        assert new_session is not old_session
        assert new_session is not None
        assert new_session["session_id"] == "job-002"
        assert mod._session is not None

    def test_incomplete_session_not_logged(self):
        """Sessions without 'ready' mark are skipped in _log()."""
        import metrics.session as mod

        ColdStartMetrics.start(session_id="job-001", room_name="r1")
        ColdStartMetrics.mark("stt_init")
        ColdStartMetrics.mark("tts_init")
        ColdStartMetrics.mark("vad")
        # Missing "ready" — flush should not log

        ColdStartMetrics.flush()
        assert mod._session is None

    def test_set_meta(self):
        import metrics.session as mod

        ColdStartMetrics.start(session_id="job-001", room_name="r1")
        ColdStartMetrics.set_meta("stt_provider", "deepgram")
        ColdStartMetrics.set_meta("llm_model", "qwen-plus")
        ColdStartMetrics.mark("ready")

        assert mod._session is not None
        assert mod._session["meta"]["stt_provider"] == "deepgram"
        assert mod._session["meta"]["llm_model"] == "qwen-plus"

    def test_entry_mark_set_on_start(self):
        """start() automatically marks 'entry'."""
        import metrics.session as mod

        ColdStartMetrics.start(session_id="job-001", room_name="r1")
        assert "entry" in mod._session["marks"]


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
