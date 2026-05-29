"""Tests for core.audio.tts — create_tts factory and CartesiaTTS."""

import os
from unittest.mock import patch

import pytest


class TestCreateTTS:
    def test_requires_api_key(self):
        from core.audio.tts import create_tts
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("CARTESIA_API_KEY", None)
            with pytest.raises(ValueError, match="CARTESIA_API_KEY"):
                create_tts()

    def test_uses_env_api_key(self):
        from core.audio.tts import create_tts, TTSProvider
        with patch("core.audio.tts.cartesia.TTS", return_value="fake"):
            tts = create_tts(api_key="test-key")
            assert isinstance(tts, TTSProvider)
            assert tts.get_plugin() == "fake"

    def test_defaults(self):
        from core.audio.tts import create_tts
        with patch("core.audio.tts.cartesia.TTS") as mock:
            create_tts(api_key="k")
        kwargs = mock.call_args.kwargs
        assert kwargs["model"] == "sonic-3.5"
        assert kwargs["language"] == "en"
        assert kwargs["sample_rate"] == 24000

    def test_passes_all_params(self):
        from core.audio.tts import create_tts
        with patch("core.audio.tts.cartesia.TTS") as mock:
            create_tts(api_key="k", speed=1.5, emotion=["happy"], volume=0.8,
                       word_timestamps=False, pronunciation_dict_id="dict-1")
        kwargs = mock.call_args.kwargs
        assert kwargs["speed"] == 1.5
        assert kwargs["emotion"] == ["happy"]
        assert kwargs["volume"] == 0.8
        assert kwargs["word_timestamps"] is False
        assert kwargs["pronunciation_dict_id"] == "dict-1"

    def test_custom_model(self):
        from core.audio.tts import create_tts
        with patch("core.audio.tts.cartesia.TTS") as mock:
            create_tts(api_key="k", model="sonic-3")
        assert mock.call_args.kwargs["model"] == "sonic-3"
