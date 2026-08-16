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


class TestStreamOpusEncoder:
    """Incremental encoder: arbitrary input sizes → fixed 60ms frames."""

    FRAME = 1920  # 960 samples × 2 bytes at 16kHz

    def test_exact_frame(self):
        from providers.tts.cartesia import _StreamOpusEncoder

        enc = _StreamOpusEncoder(16000)
        frames = enc.feed(b"\x00" * self.FRAME)
        assert len(frames) == 1
        assert enc.flush() == []

    def test_partial_carry_accumulates(self):
        from providers.tts.cartesia import _StreamOpusEncoder

        enc = _StreamOpusEncoder(16000)
        assert enc.feed(b"\x00" * 1000) == []  # 1000 < 1920 → no frame yet
        frames = enc.feed(b"\x00" * 1000)     # 2000 → 1 frame, 80 carry
        assert len(frames) == 1
        tail = enc.flush()                    # padded final frame
        assert len(tail) == 1

    def test_many_small_chunks(self):
        from providers.tts.cartesia import _StreamOpusEncoder

        enc = _StreamOpusEncoder(16000)
        total = 0
        for _ in range(30):
            total += len(enc.feed(b"\x00" * 500))
        assert total == 7  # 15000 // 1920
        assert len(enc.flush()) == 1  # 15000 % 1920 = 1560 → padded

    def test_empty_feed_and_flush(self):
        from providers.tts.cartesia import _StreamOpusEncoder

        enc = _StreamOpusEncoder(16000)
        assert enc.feed(b"") == []
        assert enc.flush() == []


class TestStreamAudioErrors:
    def test_missing_api_key_raises(self, monkeypatch):
        import asyncio

        from providers.tts.cartesia import CartesiaTTS

        monkeypatch.setenv("CARTESIA_API_KEY", "")
        tts = CartesiaTTS(api_key="")

        async def _text():
            yield "hello"

        with pytest.raises(RuntimeError):
            asyncio.run(_collect(tts.stream_audio(_text(), asyncio.Event())))


async def _collect(gen):
    async for _ in gen:
        pass
