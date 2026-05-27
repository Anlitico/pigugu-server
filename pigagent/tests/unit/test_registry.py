# tests/unit/test_registry.py
"""Unit tests for ModelRegistry, provider config loading, and model loading."""

import os
import tempfile
from pathlib import Path

import pytest

# -- ModelRegistry tests ------------------------------------------------------

from core.llm.registry import ModelRegistry
from core.llm.types import ModelInfo, ModelCapability


class TestModelRegistry:
    def setup_method(self):
        ModelRegistry._models.clear()

    def test_register_and_get(self):
        info = ModelInfo(
            model_id="test-model",
            provider="test",
            display_name="Test Model",
            capabilities={ModelCapability.TEXT, ModelCapability.STREAMING},
            context_window=4096,
            max_output_tokens=1024,
            thinking=False,
            search=False,
        )
        ModelRegistry.register(info)
        result = ModelRegistry.get("test-model")
        assert result.model_id == "test-model"
        assert result.provider == "test"
        assert result.context_window == 4096

    def test_get_unknown_fallback(self):
        """Unknown model returns a fallback ModelInfo, not None."""
        result = ModelRegistry.get("nonexistent")
        assert result.model_id == "nonexistent"
        assert result.provider == "unknown"
        assert ModelCapability.TEXT in result.capabilities

    def test_list_all(self):
        ModelRegistry.register(ModelInfo(model_id="a", provider="x",
            display_name="A", capabilities={ModelCapability.TEXT}))
        ModelRegistry.register(ModelInfo(model_id="b", provider="x",
            display_name="B", capabilities={ModelCapability.TEXT, ModelCapability.STREAMING}))
        assert len(ModelRegistry.list()) == 2

    def test_list_filter_by_provider(self):
        ModelRegistry.register(ModelInfo(model_id="a", provider="x",
            display_name="A", capabilities={ModelCapability.TEXT}))
        ModelRegistry.register(ModelInfo(model_id="b", provider="y",
            display_name="B", capabilities={ModelCapability.TEXT}))
        result = ModelRegistry.list(provider="x")
        assert len(result) == 1
        assert result[0].model_id == "a"

    def test_list_filter_by_capability(self):
        ModelRegistry.register(ModelInfo(model_id="a", provider="x",
            display_name="A", capabilities={ModelCapability.TEXT}))
        ModelRegistry.register(ModelInfo(model_id="b", provider="x",
            display_name="B", capabilities={ModelCapability.TEXT, ModelCapability.TOOL_USE}))
        result = ModelRegistry.list(capability=ModelCapability.TOOL_USE)
        assert len(result) == 1
        assert result[0].model_id == "b"


# -- Provider config tests ---------------------------------------------------

from core.llm.registry import get_provider_config, resolve_provider


class TestProviderConfig:
    """Provider config is loaded from providers.toml at import time."""

    def test_get_known_provider(self):
        cfg = get_provider_config("qwen-us")
        assert cfg is not None
        assert "dashscope-us" in cfg.base_url
        assert cfg.env == "DASHSCOPE_US_API_KEY"

    def test_get_unknown_provider(self):
        cfg = get_provider_config("nonexistent-provider")
        assert cfg is None

    def test_resolve_provider(self):
        base_url, api_key, default = resolve_provider("qwen-us")
        assert "dashscope-us" in base_url
        assert default == "qwen-plus-us"

    def test_resolve_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown provider"):
            resolve_provider("nonexistent")

    def test_provider_config_has_backend(self):
        for pid in ("qwen-us", "qwen-cn", "volcengine"):
            cfg = get_provider_config(pid)
            if cfg is None:
                pytest.fail(f"Provider {pid} not found")
            assert cfg.backend, f"Missing backend for {pid}"

    def test_qwen_backend_uses_qwen_provider_class(self):
        cfg = get_provider_config("qwen-cn")
        assert cfg is not None
        assert "QwenProvider" in cfg.backend

    def test_volcengine_backend_uses_volcengine_class(self):
        cfg = get_provider_config("volcengine")
        assert cfg is not None
        assert "VolcengineProvider" in cfg.backend

    def test_load_class_reflection(self):
        from core.llm import _load_class
        cls = _load_class("core.llm.providers.qwen.QwenProvider")
        assert cls.__name__ == "QwenProvider"

    def test_load_class_bad_path_raises(self):
        from core.llm import _load_class
        with pytest.raises((ImportError, AttributeError, ModuleNotFoundError)):
            _load_class("nonexistent.module.FakeClass")


# -- TOML model loading tests -------------------------------------------------

from core.llm.registry import load_models

SAMPLE_TOML = b"""
[[models]]
id = "test-qwen"
provider = "qwen-us"
display = "Test Qwen"
context = 1000000
output = 32768
capabilities = ["text", "streaming", "tool_use"]
thinking = true
search = true

[[models]]
id = "test-basic"
provider = "deepseek"
display = "Test Basic"
context = 4096
output = 1024
capabilities = ["text"]
thinking = false
search = false
"""


class TestLoadModels:
    def setup_method(self):
        ModelRegistry._models.clear()

    def teardown_method(self):
        # Restore real models so other tests aren't affected
        load_models()

    def test_load_from_file(self):
        with tempfile.NamedTemporaryFile(suffix=".toml", delete=False) as f:
            f.write(SAMPLE_TOML)
            f.flush()
            path = f.name

        try:
            count = load_models(path)
            assert count == 2

            m1 = ModelRegistry.get("test-qwen")
            assert m1.provider == "qwen-us"
            assert m1.context_window == 1000000
            assert m1.max_output_tokens == 32768
            assert m1.thinking is True
            assert m1.search is True
            assert ModelCapability.TOOL_USE in m1.capabilities

            m2 = ModelRegistry.get("test-basic")
            assert m2.provider == "deepseek"
            assert m2.context_window == 4096
            assert m2.thinking is False
            assert m2.search is False
            assert ModelCapability.TOOL_USE not in m2.capabilities
        finally:
            os.unlink(path)

    def test_load_missing_file(self):
        count = load_models("/nonexistent/path/models.toml")
        assert count == 0


# -- Resolve provider (env var) test ------------------------------------------

class TestResolveProviderEnv:
    def test_api_key_from_env(self, monkeypatch):
        monkeypatch.setenv("DASHSCOPE_US_API_KEY", "sk-test-key")
        _, api_key, _ = resolve_provider("qwen-us")
        assert api_key == "sk-test-key"

    def test_api_key_empty_when_not_set(self, monkeypatch):
        monkeypatch.delenv("DASHSCOPE_US_API_KEY", raising=False)
        _, api_key, _ = resolve_provider("qwen-us")
        assert api_key == ""


# -- list_providers test -----------------------------------------------------

from core.llm.registry import list_providers


class TestListProviders:
    def test_returns_known_providers(self):
        providers = list_providers()
        assert "qwen-us" in providers
        assert "volcengine" in providers

    def test_case_sensitive_lookup_still_works(self):
        # list_providers returns the canonical names from providers.toml
        cfg = get_provider_config("QWEN-US")
        assert cfg is not None
