# tests/unit/test_factory.py
"""Unit tests for bootstrap/factory.py  -  singleton creation, validation."""

import os
from unittest.mock import MagicMock, patch

import pytest

import pytest


class TestValidateConfiguration:
    def test_missing_livekit_key_fails(self, monkeypatch):
        monkeypatch.delenv("LIVEKIT_API_KEY", raising=False)
        monkeypatch.setenv("LIVEKIT_API_SECRET", "test")
        monkeypatch.setenv("CARTESIA_API_KEY", "test")
        monkeypatch.setenv("DASHSCOPE_US_API_KEY", "test")

        from bootstrap.factory import validate_configuration
        config = MagicMock()
        config.STT_PROVIDER = "cartesia"
        config.resolve_model = MagicMock(return_value="qwen-plus-us")
        config.ENABLE_POLICY_SEARCH = False

        with patch("core.llm.registry.get_provider_config") as mock_cfg:
            mock_cfg.return_value = MagicMock(env="DASHSCOPE_US_API_KEY")
            result = validate_configuration(config)
            assert not result

    def test_all_keys_present_passes(self, monkeypatch):
        monkeypatch.setenv("LIVEKIT_API_KEY", "test")
        monkeypatch.setenv("LIVEKIT_API_SECRET", "test")
        monkeypatch.setenv("CARTESIA_API_KEY", "test")
        monkeypatch.setenv("DASHSCOPE_US_API_KEY", "test")

        from bootstrap.factory import validate_configuration
        config = MagicMock()
        config.STT_PROVIDER = "cartesia"
        config.resolve_model = MagicMock(return_value="qwen-plus-us")
        config.ENABLE_POLICY_SEARCH = False

        with patch("core.llm.registry.get_provider_config") as mock_cfg:
            mock_cfg.return_value = MagicMock(env="DASHSCOPE_US_API_KEY")
            result = validate_configuration(config)
            assert result

    def test_unknown_llm_provider_reported(self, monkeypatch):
        monkeypatch.setenv("LIVEKIT_API_KEY", "test")
        monkeypatch.setenv("LIVEKIT_API_SECRET", "test")
        monkeypatch.setenv("CARTESIA_API_KEY", "test")

        from bootstrap.factory import validate_configuration
        config = MagicMock()
        config.STT_PROVIDER = "cartesia"
        config.resolve_model = MagicMock(return_value="nonexistent-model")
        config.ENABLE_POLICY_SEARCH = False

        with patch("core.llm.registry.get_provider_config", return_value=None):
            result = validate_configuration(config)
            assert not result

    def test_deepgram_missing_key(self, monkeypatch):
        monkeypatch.setenv("LIVEKIT_API_KEY", "test")
        monkeypatch.setenv("LIVEKIT_API_SECRET", "test")
        monkeypatch.setenv("CARTESIA_API_KEY", "test")
        monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
        monkeypatch.setenv("DASHSCOPE_US_API_KEY", "test")

        from bootstrap.factory import validate_configuration
        config = MagicMock()
        config.STT_PROVIDER = "deepgram"
        config.resolve_model = MagicMock(return_value="qwen-plus-us")
        config.ENABLE_POLICY_SEARCH = False

        with patch("core.llm.registry.get_provider_config") as mock_cfg:
            mock_cfg.return_value = MagicMock(env="DASHSCOPE_US_API_KEY")
            result = validate_configuration(config)
            assert not result


class TestGetRedis:
    def test_requires_env_var(self, monkeypatch):
        monkeypatch.delenv("REDIS_URL", raising=False)
        import bootstrap.factory as f
        f._redis = None

        with pytest.raises(RuntimeError, match="REDIS_URL"):
            f._init_redis()


class TestGetPgPool:
    @pytest.mark.asyncio
    async def test_requires_env_var(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        import context.storage.pg as pg_mod
        pg_mod._pool = None

        with pytest.raises(RuntimeError, match="DATABASE_URL"):
            await pg_mod._ensure_pg_pool()
