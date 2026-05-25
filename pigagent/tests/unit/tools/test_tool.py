"""Tests for core.agent.tool  -  Tool dataclass."""

import pytest

from core.agent.tool import Tool
from core.llm.types import ToolSpec


def make_test_handler(result="ok"):
    async def handler(args: dict) -> str:
        return result
    return handler


class TestTool:
    def test_creates_with_all_fields(self):
        handler = make_test_handler()
        tool = Tool(
            name="test_tool",
            description="A test tool.",
            parameters={"type": "object", "properties": {}},
            execute=handler,
        )
        assert tool.name == "test_tool"
        assert tool.description == "A test tool."
        assert tool.parameters == {"type": "object", "properties": {}}
        assert tool.execute is handler

    def test_spec_returns_tool_spec(self):
        tool = Tool(
            name="my_tool",
            description="Does something.",
            parameters={"type": "object", "properties": {"x": {"type": "string"}}},
            execute=make_test_handler(),
        )
        spec = tool.spec
        assert isinstance(spec, ToolSpec)
        assert spec.name == "my_tool"
        assert spec.description == "Does something."
        assert spec.parameters == tool.parameters

    def test_spec_to_openai_schema(self):
        tool = Tool(
            name="my_tool",
            description="Does something.",
            parameters={
                "type": "object",
                "properties": {"q": {"type": "string", "description": "Query"}},
                "required": ["q"],
            },
            execute=make_test_handler(),
        )
        schema = tool.spec.to_openai_schema()
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "my_tool"
        assert schema["function"]["description"] == "Does something."

    def test_frozen_prevents_mutation(self):
        tool = Tool(
            name="t", description="d", parameters={}, execute=make_test_handler()
        )
        with pytest.raises(Exception):
            tool.name = "new_name"  # type: ignore[misc]

    def test_description_is_exact(self):
        """Description must be the exact hand-written string, not parsed or modified."""
        desc = "Search the web. Use for recent events and news."
        tool = Tool(
            name="search",
            description=desc,
            parameters={},
            execute=make_test_handler(),
        )
        assert tool.description == desc
