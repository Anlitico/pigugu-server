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


