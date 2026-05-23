# tests/unit/core/search/test_perplexity.py
"""Tests for core.search.perplexity — API key resolution, URL inference, search result."""


class TestGetPerplexityApiKey:
    def test_perplexity_key(self, monkeypatch):
        from core.search.perplexity import get_perplexity_api_key
        monkeypatch.setenv("PERPLEXITY_API_KEY", "pplx-test")
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        assert get_perplexity_api_key() == "pplx-test"

    def test_openrouter_fallback(self, monkeypatch):
        from core.search.perplexity import get_perplexity_api_key
        monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        assert get_perplexity_api_key() == "sk-or-test"

    def test_perplexity_priority(self, monkeypatch):
        from core.search.perplexity import get_perplexity_api_key
        monkeypatch.setenv("PERPLEXITY_API_KEY", "pplx-first")
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-second")
        assert get_perplexity_api_key() == "pplx-first"

    def test_none_when_unset(self, monkeypatch):
        from core.search.perplexity import get_perplexity_api_key
        monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        assert get_perplexity_api_key() is None


class TestInferBaseUrlFromKey:
    def test_pplx_prefix(self):
        from core.search.perplexity import infer_base_url_from_key
        assert infer_base_url_from_key("pplx-abc123") == "https://api.perplexity.ai"

    def test_sk_or_prefix(self):
        from core.search.perplexity import infer_base_url_from_key
        assert infer_base_url_from_key("sk-or-xyz") == "https://openrouter.ai/api/v1"

    def test_unknown_prefix(self):
        from core.search.perplexity import infer_base_url_from_key
        assert infer_base_url_from_key("unknown-key") == ""

    def test_empty_string(self):
        from core.search.perplexity import infer_base_url_from_key
        assert infer_base_url_from_key("") == ""


class TestPerplexitySearchResult:
    def test_defaults(self):
        from core.search.perplexity import PerplexitySearchResult
        r = PerplexitySearchResult(query="q", content="c", citations=[], model="m")
        assert r.provider == "perplexity"
        assert r.took_ms is None

    def test_full(self):
        from core.search.perplexity import PerplexitySearchResult
        r = PerplexitySearchResult(
            query="q", content="c", citations=["a", "b"],
            model="sonar", provider="openrouter", took_ms=123,
        )
        assert r.citations == ["a", "b"]
        assert r.provider == "openrouter"
        assert r.took_ms == 123
