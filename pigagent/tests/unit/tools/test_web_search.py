"""Tests for tools.web_search  -  create_web_search_tool factory."""

import asyncio

from core.agent.registry import ToolRegistry
from core.llm.types import ToolSpec
from tools.search.base import SearchProvider, SearchResult
from tools.web_search import create_web_search_tool


class FakeProvider(SearchProvider):
    """Mock SearchProvider that returns canned results."""

    async def search(self, query: str) -> SearchResult:
        return SearchResult(
            content=f"Results for: {query}",
            citations=["https://example.com/1", "https://example.com/2"],
            provider="fake",
            model="fake-v1",
            took_ms=100,
        )


class TestCreateWebSearchTool:
    def test_tool_name(self):
        tool = create_web_search_tool(FakeProvider())
        assert tool.name == "web_search"

    def test_description_is_hand_written(self):
        tool = create_web_search_tool(FakeProvider())
        assert "Search the web" in tool.description
        assert "synthesized answers" in tool.description

    def test_parameters_schema(self):
        tool = create_web_search_tool(FakeProvider())
        params = tool.parameters
        assert params["type"] == "object"
        assert "query" in params["properties"]
        assert params["properties"]["query"]["type"] == "string"
        assert "user_reply" in params.get("required", [])
        assert "query" in params.get("required", [])

    def test_spec_is_tool_spec(self):
        tool = create_web_search_tool(FakeProvider())
        spec = tool.spec
        assert isinstance(spec, ToolSpec)
        assert spec.name == "web_search"

    def test_spec_to_openai_schema(self):
        tool = create_web_search_tool(FakeProvider())
        schema = tool.spec.to_openai_schema()
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "web_search"

    def test_execute_is_callable(self):
        tool = create_web_search_tool(FakeProvider())
        assert callable(tool.execute)

    def test_execute_delegates_to_provider(self):
        tool = create_web_search_tool(FakeProvider())
        result = asyncio.run(tool.execute({"query": "test query"}))
        assert result["content"] == "Results for: test query"
        assert result["citations"] == ["https://example.com/1", "https://example.com/2"]

    def test_handler_requires_query_arg(self):
        tool = create_web_search_tool(FakeProvider())
        with __import__("pytest").raises(KeyError):
            asyncio.run(tool.execute({}))


class TestWebSearchRegistration:
    def test_registers_into_registry(self):
        tool = create_web_search_tool(FakeProvider())
        registry = ToolRegistry()
        registry.register(tool)
        assert len(registry) == 1
        assert "web_search" in registry

    def test_registered_tool_is_web_search(self):
        tool = create_web_search_tool(FakeProvider())
        registry = ToolRegistry()
        registry.register(tool)
        registered = registry.get("web_search")
        assert registered is not None
        assert registered.name == "web_search"
        assert callable(registered.execute)

    def test_registry_tools_has_spec(self):
        tool = create_web_search_tool(FakeProvider())
        registry = ToolRegistry()
        registry.register(tool)
        assert len(registry.tools) == 1
        assert registry.tools[0].name == "web_search"

    def test_registry_handlers_has_execute(self):
        tool = create_web_search_tool(FakeProvider())
        registry = ToolRegistry()
        registry.register(tool)
        assert "web_search" in registry.tool_handlers
        assert callable(registry.tool_handlers["web_search"])
