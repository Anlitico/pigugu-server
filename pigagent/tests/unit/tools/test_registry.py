"""Tests for core.agent.registry  -  ToolRegistry."""

import pytest

from core.agent.registry import ToolRegistry
from core.agent.tool import Tool
from core.llm.types import ToolSpec


def make_tool(name="test", result="ok"):
    async def handler(args: dict) -> str:
        return result
    return Tool(
        name=name,
        description=f"Tool: {name}",
        parameters={"type": "object", "properties": {}},
        execute=handler,
    )


class TestToolRegistry:
    def test_empty_registry(self):
        r = ToolRegistry()
        assert r.tools == []
        assert r.tool_handlers == {}
        assert len(r) == 0

    def test_register_single(self):
        r = ToolRegistry()
        tool = make_tool("a")
        r.register(tool)
        assert len(r) == 1
        assert r.tools[0].name == "a"
        assert "a" in r
        assert "a" in r.tool_handlers
        assert callable(r.tool_handlers["a"])

    def test_register_many(self):
        r = ToolRegistry()
        r.register_many([make_tool("a"), make_tool("b"), make_tool("c")])
        assert len(r) == 3
        assert {t.name for t in r.tools} == {"a", "b", "c"}
        assert set(r.tool_handlers.keys()) == {"a", "b", "c"}

    def test_register_overwrites_by_name(self):
        r = ToolRegistry()
        r.register(make_tool("x", result="old"))
        r.register(make_tool("x", result="new"))
        assert len(r) == 1
        handler = r.tool_handlers["x"]
        import asyncio
        result = asyncio.run(handler({}))
        assert result == "new"

    def test_get(self):
        r = ToolRegistry()
        tool = make_tool("a")
        r.register(tool)
        assert r.get("a") is tool
        assert r.get("nonexistent") is None

    def test_tools_and_handlers_synced(self):
        r = ToolRegistry()
        r.register_many([make_tool("a"), make_tool("b")])
        assert len(r.tools) == len(r.tool_handlers) == 2
        tool_names = {t.name for t in r.tools}
        handler_names = set(r.tool_handlers.keys())
        assert tool_names == handler_names

    def test_tools_returns_tool_specs(self):
        r = ToolRegistry()
        r.register(make_tool("a"))
        spec = r.tools[0]
        assert isinstance(spec, ToolSpec)
        assert spec.name == "a"

    def test_contains(self):
        r = ToolRegistry()
        r.register(make_tool("search"))
        assert "search" in r
        assert "unknown" not in r

    def test_execute_via_registry_handler(self):
        r = ToolRegistry()
        r.register(make_tool("echo", result="hello"))
        handler = r.tool_handlers["echo"]
        import asyncio
        result = asyncio.run(handler({"msg": "hi"}))
        assert result == "hello"

    def test_multiple_tools_independent(self):
        r = ToolRegistry()
        r.register_many([
            make_tool("a", result="alpha"),
            make_tool("b", result="beta"),
        ])
        import asyncio
        assert asyncio.run(r.tool_handlers["a"]({})) == "alpha"
        assert asyncio.run(r.tool_handlers["b"]({})) == "beta"

    def test_handler_receives_args_dict(self):
        captured = {}

        async def handler(args: dict) -> dict:
            captured["args"] = args
            return {"received": True}

        tool = Tool(
            name="capture",
            description="Captures args.",
            parameters={"type": "object", "properties": {"x": {"type": "integer"}}},
            execute=handler,
        )
        r = ToolRegistry()
        r.register(tool)

        import asyncio
        result = asyncio.run(r.tool_handlers["capture"]({"x": 42}))
        assert captured["args"] == {"x": 42}
        assert result == {"received": True}
