# tests/unit/test_pool.py
"""Unit tests for provider instance pool and get_llm()."""

import pytest

from core.llm.registry import ModelRegistry, get_provider_config
from core.llm.types import ModelInfo, ModelCapability


class TestPool:
    """Tests for _build_pool() and get_llm().

    The pool is built at import time using real models.toml and providers.toml,
    so we test against the actual pool content. Pool is keyed by provider_id,
    not model_id.
    """

    def test_get_llm_returns_provider(self):
        from core.llm import get_llm, _pool, LLMProvider
        provider = get_llm("qwen-plus")
        assert isinstance(provider, LLMProvider)
        assert "qwen-us" in _pool

    def test_get_llm_different_models_same_backend_same_instance(self):
        from core.llm import get_llm
        # Both qwen-plus and qwen-flash use the same qwen-us backend
        a = get_llm("qwen-plus")
        b = get_llm("doubao-seed-1-6-251015")
        assert a is not b  # different backends
        assert a.base_url != b.base_url

    def test_same_model_same_instance(self):
        from core.llm import get_llm
        a = get_llm("qwen-plus")
        b = get_llm("qwen-plus")
        assert a is b

    def test_all_registered_models_resolvable(self):
        from core.llm import _pool
        for info in ModelRegistry.list():
            cfg = get_provider_config(info.provider)
            if cfg is None:
                continue
            assert info.provider in _pool, f"Missing pool entry for provider {info.provider}"

    def test_get_llm_unknown_raises(self):
        from core.llm import get_llm
        with pytest.raises(KeyError):
            get_llm("nonexistent-model")

    def test_create_llm_alias(self):
        from core.llm import create_llm, get_llm
        a = create_llm("qwen-plus")
        b = get_llm("qwen-plus")
        assert a is b

    def test_pool_providers_have_base_url(self):
        from core.llm import _pool
        for pid, p in _pool.items():
            assert p.base_url, f"Missing base_url for provider {pid}"
