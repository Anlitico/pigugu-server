"""Tests for core.audio.tts — create_tts factory."""

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
        from core.audio.tts import create_tts
        with patch("core.audio.tts.TTS", return_value="fake_tts"):
            tts = create_tts(api_key="test-key")
            assert tts == "fake_tts"

    def test_defaults(self):
        from core.audio.tts import create_tts
        with patch("core.audio.tts.TTS") as mock_tts:
            create_tts(api_key="k")
        call_kwargs = mock_tts.call_args.kwargs
        assert call_kwargs["model"] == "cartesia/sonic-3"
        assert call_kwargs["language"] == "en"
        assert call_kwargs["sample_rate"] == 24000

    def test_passes_max_buffer_delay_ms(self):
        from core.audio.tts import create_tts
        with patch("core.audio.tts.TTS") as mock_tts:
            create_tts(api_key="k", max_buffer_delay_ms=50)
        extra = mock_tts.call_args.kwargs["extra_kwargs"]
        assert extra["max_buffer_delay_ms"] == 50

    def test_passes_speed_emotion_volume(self):
        from core.audio.tts import create_tts
        with patch("core.audio.tts.TTS") as mock_tts:
            create_tts(api_key="k", speed=1.5, emotion=["happy"], volume=0.8)
        extra = mock_tts.call_args.kwargs["extra_kwargs"]
        assert extra["speed"] == 1.5
        assert extra["emotion"] == "happy"
        assert extra["volume"] == 0.8

    def test_skips_none_options(self):
        from core.audio.tts import create_tts
        with patch("core.audio.tts.TTS") as mock_tts:
            create_tts(api_key="k")
        extra = mock_tts.call_args.kwargs["extra_kwargs"]
        assert "speed" not in extra
        assert "emotion" not in extra
        assert "volume" not in extra

    def test_emotion_list_joined(self):
        from core.audio.tts import create_tts
        with patch("core.audio.tts.TTS") as mock_tts:
            create_tts(api_key="k", emotion=["happy", "sad"])
        extra = mock_tts.call_args.kwargs["extra_kwargs"]
        assert extra["emotion"] == "happy,sad"

    def test_normalizes_legacy_model_name(self):
        from core.audio.tts import create_tts
        with patch("core.audio.tts.TTS") as mock_tts:
            create_tts(api_key="k", model="sonic-2")
        assert mock_tts.call_args.kwargs["model"] == "cartesia/sonic-2"

    def test_preserves_full_model_name(self):
        from core.audio.tts import create_tts
        with patch("core.audio.tts.TTS") as mock_tts:
            create_tts(api_key="k", model="cartesia/sonic-3.5")
        assert mock_tts.call_args.kwargs["model"] == "cartesia/sonic-3.5"
