"""Tests for Deepgram STT provider."""

import os

import pytest


@pytest.fixture
def deepgram_stt():
    """Return a DeepgramSTT with a dummy key (never makes real calls in tests)."""
    os.environ.setdefault("DEEPGRAM_API_KEY", "test-key")
    from providers.stt.deepgram import DeepgramSTT

    return DeepgramSTT(api_key="test-key")


class TestDeepgramSTT:
    def test_empty_pcm_returns_empty(self, deepgram_stt):
        import asyncio

        result = asyncio.run(deepgram_stt.transcribe(b""))
        assert result == ""

    def test_too_short_pcm_returns_empty(self, deepgram_stt):
        import asyncio

        result = asyncio.run(deepgram_stt.transcribe(b"\x00" * 100))
        assert result == ""

    def test_missing_api_key_returns_empty(self):
        from providers.stt.deepgram import DeepgramSTT

        stt = DeepgramSTT(api_key="")
        import asyncio

        result = asyncio.run(stt.transcribe(b"\x00" * 2000))
        assert result == ""

    def test_url_construction(self, deepgram_stt):
        """Verify the Deepgram URL is constructed correctly."""
        url = (
            "https://api.deepgram.com/v1/listen"
            "?model=nova-3"
            "&language=en"
            "&encoding=linear16"
            "&sample_rate=16000"
        )
        assert "nova-3" in url
        assert "language=en" in url
        assert "linear16" in url


class TestShouldBargeIn:
    """Barge-in decision: only while TTS is playing, low bar for short interrupts."""

    def _check(self, text, speaking, expected):
        from types import SimpleNamespace

        from providers.stt.deepgram import _should_barge_in

        conn = SimpleNamespace(client_is_speaking=speaking)
        assert _should_barge_in(conn, text) == expected

    def test_no_barge_in_while_listening(self):
        # The user's own utterance — nothing to abort. Regression: long
        # interims here used to fire aborts the firmware then ignored.
        self._check("Change of job, please.", False, False)

    def test_single_word_interrupt(self):
        self._check("What", True, True)

    def test_two_word_interrupt(self):
        self._check("job, please.", True, True)

    def test_short_noise_word_ignored(self):
        self._check("um", True, False)

    def test_chinese_interrupt_without_spaces(self):
        self._check("停一下", True, True)

    def test_single_cjk_char_ignored(self):
        self._check("停", True, False)

    def test_blank_text(self):
        self._check("   ", True, False)
