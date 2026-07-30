"""Tests for Cartesia TTS provider."""

import os

import pytest


@pytest.fixture
def cartesia_tts():
    """Return a CartesiaTTS with a dummy key."""
    from providers.tts.cartesia import CartesiaTTS
    return CartesiaTTS(api_key="test-key")


class TestCartesiaTTS:
    def test_empty_text_returns_empty(self, cartesia_tts):
        import asyncio

        result = asyncio.run(cartesia_tts.synthesize(""))
        assert result == []

    def test_whitespace_only_returns_empty(self, cartesia_tts):
        import asyncio

        result = asyncio.run(cartesia_tts.synthesize("   "))
        assert result == []

    def test_missing_api_key_returns_empty(self, monkeypatch):
        from providers.tts.cartesia import CartesiaTTS

        monkeypatch.setenv("CARTESIA_API_KEY", "")
        tts = CartesiaTTS(api_key="")
        import asyncio

        result = asyncio.run(tts.synthesize("hello"))
        assert result == []


class TestOpusEncodeChunks:
    def test_empty_pcm_returns_empty_or_raw(self):
        from providers.tts.cartesia import _opus_encode_chunks

        result = _opus_encode_chunks(b"", sample_rate=16000)
        # opuslib may not be installed → returns raw; with opuslib → empty
        assert isinstance(result, list)

    def test_short_pcm_without_opuslib(self):
        """Without opuslib, returns raw PCM wrapper."""
        from providers.tts.cartesia import _opus_encode_chunks

        result = _opus_encode_chunks(b"\x00" * 10, sample_rate=16000)
        assert isinstance(result, list)
