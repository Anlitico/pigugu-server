# tests/unit/test_types.py
"""Unit tests for types — Message, ToolCall, ToolSpec, ChatResponse, TokenUsage, ModelInfo."""

from core.llm.types import (
    Message, ToolCall, ToolSpec, ChatResponse, ChatDelta,
    TokenUsage, ModelInfo, ModelCapability,
)


class TestMessage:
    def test_system(self):
        m = Message.system("You are helpful.")
        assert m.role == "system"
        assert m.content == "You are helpful."
        assert m.partial is False

    def test_user(self):
        m = Message.user("hello")
        assert m.role == "user"
        assert m.content == "hello"

    def test_assistant(self):
        m = Message.assistant("hi there")
        assert m.role == "assistant"
        assert m.content == "hi there"
        assert m.partial is False

    def test_assistant_partial(self):
        m = Message.assistant("prefix...", partial=True)
        assert m.partial is True

    def test_assistant_with_tool_calls(self):
        tc = ToolCall(id="c1", name="search", arguments='{"q":"test"}')
        m = Message.assistant("", tool_calls=[tc])
        assert m.tool_calls == [tc]

    def test_tool(self):
        m = Message.tool(call_id="c1", name="search", content="result")
        assert m.role == "tool"
        assert m.tool_call_id == "c1"
        assert m.name == "search"
        assert m.content == "result"

    def test_to_openai_dict_basic(self):
        m = Message(role="user", content="hello")
        d = m.to_openai_dict()
        assert d == {"role": "user", "content": "hello"}

    def test_to_openai_dict_with_tool_calls(self):
        tc = ToolCall(id="c1", name="search", arguments='{"q":"test"}')
        m = Message(role="assistant", content="", tool_calls=[tc])
        d = m.to_openai_dict()
        assert d["tool_calls"][0]["id"] == "c1"
        assert d["tool_calls"][0]["type"] == "function"
        assert d["tool_calls"][0]["function"]["name"] == "search"

    def test_to_openai_dict_with_tool_call_id(self):
        m = Message(role="tool", content="result", tool_call_id="c1", name="search")
        d = m.to_openai_dict()
        assert d["tool_call_id"] == "c1"
        assert d["name"] == "search"


class TestToolSpec:
    def test_to_openai_schema(self):
        spec = ToolSpec(name="get_weather", description="Get weather",
                        parameters={"type": "object", "properties": {}})
        s = spec.to_openai_schema()
        assert s["type"] == "function"
        assert s["function"]["name"] == "get_weather"
        assert s["function"]["description"] == "Get weather"
        assert "parameters" in s["function"]


class TestTokenUsage:
    def test_defaults(self):
        u = TokenUsage()
        assert u.prompt_tokens == 0
        assert u.completion_tokens == 0
        assert u.total_tokens == 0
        assert u.cached_prompt_tokens == 0

    def test_full(self):
        u = TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150,
                       cached_prompt_tokens=20, cache_write_tokens=10)
        assert u.prompt_tokens == 100
        assert u.cached_prompt_tokens == 20
        assert u.cache_write_tokens == 10


class TestChatResponse:
    def test_basic(self):
        r = ChatResponse(content="hello", finish_reason="stop")
        assert r.content == "hello"
        assert r.finish_reason == "stop"
        assert r.tool_calls is None
        assert r.usage is None

    def test_with_tool_calls(self):
        tc = ToolCall(id="c1", name="search", arguments="{}")
        r = ChatResponse(content="", tool_calls=[tc],
                         usage=TokenUsage(10, 5, 15), finish_reason="tool_calls")
        assert r.tool_calls == [tc]
        assert r.usage.total_tokens == 15


class TestChatDelta:
    def test_content(self):
        d = ChatDelta(content="hello")
        assert d.content == "hello"
        assert d.reasoning_content is None

    def test_reasoning(self):
        d = ChatDelta(reasoning_content="thinking...")
        assert d.reasoning_content == "thinking..."
        assert d.content is None

    def test_tool_calls(self):
        tc = ToolCall(id="c1", name="s", arguments="{}")
        u = TokenUsage(10, 5, 15)
        d = ChatDelta(tool_calls=[tc], usage=u, finish_reason="tool_calls")
        assert d.tool_calls == [tc]
        assert d.usage == u
        assert d.finish_reason == "tool_calls"


class TestModelInfo:
    def test_defaults(self):
        info = ModelInfo(model_id="test", provider="x", display_name="Test")
        assert info.model_id == "test"
        assert info.provider == "x"
        assert info.context_window == 0
        assert info.max_output_tokens == 0
        assert info.thinking is False
        assert info.search is False
        assert info.temperature == 0.8
        assert len(info.capabilities) == 0


class TestModelCapability:
    def test_values(self):
        assert ModelCapability.TEXT == "text"
        assert ModelCapability.TOOL_USE == "tool_use"
        assert ModelCapability.STREAMING == "streaming"
        assert ModelCapability.WEB_SEARCH == "web_search"
        assert ModelCapability.VISION == "vision"
