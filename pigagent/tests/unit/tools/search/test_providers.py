"""Tests for tools.search providers — Perplexity and Tavily."""

import pytest

from tools.search.base import SearchProvider, SearchResult
from tools.search.perplexity import PerplexityProvider, _get_api_key, _resolve_base_url
from tools.search.tavily import TavilyProvider


class TestSearchResult:
    def test_defaults(self):
        r = SearchResult(content="c", citations=[], provider="test", model="m")
        assert r.content == "c"
        assert r.citations == []
        assert r.took_ms is None

    def test_full(self):
        r = SearchResult(
            content="answer", citations=["a", "b"],
            provider="perplexity", model="sonar", took_ms=350,
        )
        assert r.citations == ["a", "b"]
        assert r.provider == "perplexity"
        assert r.took_ms == 350


class TestPerplexityKeyResolution:
    def test_perplexity_key(self, monkeypatch):
        monkeypatch.setenv("PERPLEXITY_API_KEY", "pplx-test")
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        assert _get_api_key() == "pplx-test"

    def test_openrouter_fallback(self, monkeypatch):
        monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        assert _get_api_key() == "sk-or-test"

    def test_perplexity_priority(self, monkeypatch):
        monkeypatch.setenv("PERPLEXITY_API_KEY", "pplx-first")
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-second")
        assert _get_api_key() == "pplx-first"

    def test_none_when_unset(self, monkeypatch):
        monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        assert _get_api_key() is None


class TestResolveBaseUrl:
    def test_pplx_prefix(self):
        assert _resolve_base_url("pplx-abc123") == "https://api.perplexity.ai"

    def test_sk_or_prefix(self):
        assert _resolve_base_url("sk-or-xyz") == "https://openrouter.ai/api/v1"

    def test_unknown_prefix(self):
        assert _resolve_base_url("unknown-key") == "https://api.perplexity.ai"

    def test_empty_string(self):
        assert _resolve_base_url("") == "https://api.perplexity.ai"


class TestPerplexityProvider:
    def test_implements_search_provider(self):
        assert issubclass(PerplexityProvider, SearchProvider)

    def test_init_with_key(self, monkeypatch):
        monkeypatch.setenv("PERPLEXITY_API_KEY", "pplx-test")
        provider = PerplexityProvider(api_key="pplx-test")
        assert provider._model == "sonar"

    def test_init_without_key_raises(self, monkeypatch):
        monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        with pytest.raises(RuntimeError):
            PerplexityProvider()
