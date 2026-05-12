# tests/unit/test_pool.py
"""Unit tests for provider instance pool and get_llm()."""

import pytest

from core.llm.registry import ModelRegistry, get_provider_config
from core.llm.types import ModelInfo, ModelCapability


class TestPool:
    """Tests for _build_pool() and get_llm().

    The pool is built at import time using real models.toml and providers.toml,
    so we test against the actual pool content.
    """

    def test_get_llm_returns_provider(self):
        from core.llm import get_llm, _pool, LLMProvider
        provider = get_llm("qwen-plus")
        assert isinstance(provider, LLMProvider)
        assert provider.model == "qwen-plus"
        assert "qwen-plus" in _pool

    def test_get_llm_different_models_different_instances(self):
        from core.llm import get_llm
        a = get_llm("qwen-plus")
        b = get_llm("doubao-seed-1-6-251015")
        assert a is not b
        assert a.model == "qwen-plus"
        assert b.model == "doubao-seed-1-6-251015"

    def test_same_model_same_instance(self):
        from core.llm import get_llm
        a = get_llm("qwen-plus")
        b = get_llm("qwen-plus")
        assert a is b

    def test_all_registered_models_in_pool(self):
        from core.llm import _pool
        for info in ModelRegistry.list():
            cfg = get_provider_config(info.provider)
            if cfg is None:
                continue
            assert info.model_id in _pool, f"Missing pool entry for {info.model_id}"

    def test_get_llm_unknown_raises(self):
        from core.llm import get_llm
        with pytest.raises(KeyError, match="not found in provider pool"):
            get_llm("nonexistent-model")

    def test_create_llm_alias(self):
        from core.llm import create_llm, get_llm
        a = create_llm("qwen-plus")
        b = get_llm("qwen-plus")
        assert a is b

    def test_pool_instances_have_correct_model(self):
        from core.llm import get_llm
        for model_id in ["qwen-plus", "doubao-seed-1-6-251015"]:
            p = get_llm(model_id)
            assert p.model == model_id, f"Wrong model for {model_id}: got {p.model}"
