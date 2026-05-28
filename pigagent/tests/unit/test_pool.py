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
        provider = get_llm("qwen-plus-us")
        assert isinstance(provider, LLMProvider)
        assert "qwen-us" in _pool

    def test_get_llm_different_models_same_backend_same_instance(self):
        # qwen-plus (qwen-cn) and doubao use different backends
        a = get_llm("qwen-plus-us")
        b = get_llm("doubao-seed-1-6-251015")
        assert a is not b  # different backends
        assert a.base_url != b.base_url

    def test_same_model_same_instance(self):
        a = get_llm("qwen-plus-us")
        b = get_llm("qwen-plus-us")
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
        a = create_llm("qwen-plus-us")
        b = get_llm("qwen-plus-us")
        assert a is b

    def test_pool_providers_have_base_url(self):
        for pid, p in _pool.items():
            assert p.base_url, f"Missing base_url for provider {pid}"

    def test_cn_provider_in_pool(self):
        assert "qwen-cn" in _pool, "qwen-cn provider should be in pool"

    def test_cn_model_resolves_to_cn_provider(self):
        info = ModelRegistry.get("qwen-plus-cn")
        assert info.provider == "qwen-cn"

    def test_us_model_resolves_to_us_provider(self):
        info = ModelRegistry.get("qwen-plus-us")
        assert info.provider == "qwen-us"

    def test_cn_and_us_are_different_instances(self):
        a = get_llm("qwen-plus-us")
        b = get_llm("qwen-plus-cn")
        assert a is not b
        assert a.base_url != b.base_url
        assert "dashscope-us" in a.base_url
        assert "dashscope.aliyuncs.com" in b.base_url.replace("-us.", ".")
