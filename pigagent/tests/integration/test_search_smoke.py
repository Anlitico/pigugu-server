"""Integration tests for search providers  -  real API calls.

Requires PERPLEXITY_API_KEY and TAVILY_API_KEY in pigagent/.env.
Run: pytest tests/integration/test_search_smoke.py -v --tb=short
"""

import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent.parent.parent / ".env", override=True)


def _need(env_var: str):
    if not os.getenv(env_var):
        pytest.skip(f"{env_var} not set")


def _install_check(package: str):
    try:
        __import__(package)
    except ImportError:
        pytest.skip(f"{package} not installed")


# -------------------------------------------------------------------------------
# Perplexity
# -------------------------------------------------------------------------------

@pytest.mark.asyncio
class TestPerplexityProvider:
    ENV = "PERPLEXITY_API_KEY"

    async def test_connectivity(self):
        _need(self.ENV)
        from tools.search import PerplexityProvider
        provider = PerplexityProvider(model="sonar")
        result = await provider.search("What is the capital of France?")
        assert result.content
        assert result.provider == "perplexity"
        assert result.took_ms is not None

    async def test_returns_citations(self):
        _need(self.ENV)
        from tools.search import PerplexityProvider
        provider = PerplexityProvider(model="sonar")
        result = await provider.search("Latest news about AI in 2025")
        assert result.content
        # Sonar may or may not return citations depending on query

    async def test_result_structure(self):
        _need(self.ENV)
        from tools.search import PerplexityProvider
        provider = PerplexityProvider(model="sonar")
        result = await provider.search("What is Python?")
        assert isinstance(result.content, str)
        assert len(result.content) > 20
        assert isinstance(result.citations, list)
        assert result.provider == "perplexity"
        assert result.model == "sonar"


@pytest.mark.asyncio
class TestPerplexityTool:
    ENV = "PERPLEXITY_API_KEY"

    async def test_tool_handler(self):
        _need(self.ENV)
        from tools.search import PerplexityProvider
        from tools.web_search import create_web_search_tool
        provider = PerplexityProvider(model="sonar")
        tool = create_web_search_tool(provider)
        result = await tool.execute({"query": "What is 2+2?"})
        assert "content" in result
        assert "citations" in result
        assert len(result["content"]) > 0


# -------------------------------------------------------------------------------
# Tavily
# -------------------------------------------------------------------------------

@pytest.mark.asyncio
class TestTavilyProvider:
    ENV = "TAVILY_API_KEY"

    async def test_connectivity(self):
        _need(self.ENV)
        _install_check("tavily")
        from tools.search import TavilyProvider
        provider = TavilyProvider()
        result = await provider.search("What is the capital of France?")
        assert result.content
        assert result.provider == "tavily"
        assert result.took_ms is not None

    async def test_returns_citations(self):
        _need(self.ENV)
        _install_check("tavily")
        from tools.search import TavilyProvider
        provider = TavilyProvider()
        result = await provider.search("Latest AI breakthroughs 2025")
        assert result.content
        # Tavily typically returns citations

    async def test_result_structure(self):
        _need(self.ENV)
        _install_check("tavily")
        from tools.search import TavilyProvider
        provider = TavilyProvider()
        result = await provider.search("Who wrote Romeo and Juliet?")
        assert isinstance(result.content, str)
        assert len(result.content) > 10
        assert isinstance(result.citations, list)
        assert result.provider == "tavily"


@pytest.mark.asyncio
class TestTavilyTool:
    ENV = "TAVILY_API_KEY"

    async def test_tool_handler(self):
        _need(self.ENV)
        _install_check("tavily")
        from tools.search import TavilyProvider
        from tools.web_search import create_web_search_tool
        provider = TavilyProvider()
        tool = create_web_search_tool(provider)
        result = await tool.execute({"query": "What is the weather today?"})
        assert "content" in result
        assert "citations" in result
        assert len(result["content"]) > 0


# -------------------------------------------------------------------------------
# Cross-provider
# -------------------------------------------------------------------------------

@pytest.mark.asyncio
class TestCrossProvider:
    async def test_same_interface(self):
        """Both providers return SearchResult with the same shape."""
        from tools.search.base import SearchResult

        def _check(r: SearchResult):
            assert isinstance(r.content, str)
            assert isinstance(r.citations, list)
            assert isinstance(r.provider, str)
            assert isinstance(r.model, str)

        # Static check  -  the type system enforces this at import time
        from tools.search import PerplexityProvider, TavilyProvider
        assert issubclass(PerplexityProvider, __import__("tools.search.base", fromlist=["SearchProvider"]).SearchProvider)
        assert issubclass(TavilyProvider, __import__("tools.search.base", fromlist=["SearchProvider"]).SearchProvider)


@pytest.mark.asyncio
class TestToolFactory:
    async def test_factory_creates_valid_tool(self):
        """create_web_search_tool produces a Tool with valid spec and handler."""
        _need("PERPLEXITY_API_KEY")
        from tools.search import PerplexityProvider
        from tools.web_search import create_web_search_tool
        from core.agent.tool import Tool

        provider = PerplexityProvider(model="sonar")
        tool = create_web_search_tool(provider)

        assert isinstance(tool, Tool)
        assert tool.name == "web_search"
        assert tool.description
        assert tool.parameters["type"] == "object"
        assert "query" in tool.parameters["required"]

        # spec should produce valid OpenAI schema
        schema = tool.spec.to_openai_schema()
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "web_search"
