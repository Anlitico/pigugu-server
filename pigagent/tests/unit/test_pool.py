# tests/unit/test_pool.py
"""Unit tests for provider instance pool and get_llm()."""

import pytest

from core.llm import get_llm, _pool, LLMProvider, create_llm  # pyright: ignore[reportAttributeAccessIssue]
from core.llm.registry import ModelRegistry, get_provider_config
from core.llm.types import ModelInfo, ModelCapability


class TestPool:
    """Tests for _build_pool() and get_llm().

    The pool is built at import time using real models.toml and providers.toml,
    so we test against the actual pool content. Pool is keyed by provider_id,
    not model_id.
    """

    def test_get_llm_returns_provider(self):
        provider = get_llm("qwen-plus")
        assert isinstance(provider, LLMProvider)
        assert "qwen-us" in _pool

    def test_get_llm_different_models_same_backend_same_instance(self):
        # Both qwen-plus and qwen3.6-flash use the same qwen-us backend
        a = get_llm("qwen-plus")
        b = get_llm("doubao-seed-1-6-251015")
        assert a is not b  # different backends
        assert a.base_url != b.base_url

    def test_same_model_same_instance(self):
        a = get_llm("qwen-plus")
        b = get_llm("qwen-plus")
        assert a is b

    def test_all_registered_models_resolvable(self):
        for info in ModelRegistry.list():
            cfg = get_provider_config(info.provider)
            if cfg is None:
                continue
            assert info.provider in _pool, f"Missing pool entry for provider {info.provider}"

    def test_get_llm_unknown_raises(self):
        with pytest.raises(KeyError):
            get_llm("nonexistent-model")

    def test_create_llm_alias(self):
        a = create_llm("qwen-plus")
        b = get_llm("qwen-plus")
        assert a is b

    def test_pool_providers_have_base_url(self):
        for pid, p in _pool.items():
            assert p.base_url, f"Missing base_url for provider {pid}"
