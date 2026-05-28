# tests/unit/core/llm/test_provider.py
"""Tests for LLMProvider base class  -  count_tokens and _tokenize."""

import asyncio

from core.llm.types import Message, ToolCall


class TestCountTokens:
    def test_empty_input(self):
        provider = _ConcreteProvider()
        assert asyncio.run(provider.count_tokens("")) == 0
        assert asyncio.run(provider.count_tokens([])) == 0

    def test_string(self):
        tokens = asyncio.run(_ConcreteProvider().count_tokens("hello world"))
        assert tokens > 0

    def test_single_message(self):
        msg = Message.user("hello")
        tokens = asyncio.run(_ConcreteProvider().count_tokens(msg))
        assert tokens >= 4  # overhead + content

    def test_message_list(self):
        msgs = [Message.user("hi"), Message.assistant("hello")]
        tokens = asyncio.run(_ConcreteProvider().count_tokens(msgs))
        assert tokens >= 8  # 2 × overhead + content

    def test_message_with_tool_calls(self):
        tc = ToolCall(id="c1", name="search", arguments='{"q":"test"}')
        msg = Message.assistant("", tool_calls=[tc])
        tokens = asyncio.run(_ConcreteProvider().count_tokens(msg))
        assert tokens > 4

    def test_message_with_tool_call_id(self):
        msg = Message.tool(call_id="c1", name="search", content="result")
        tokens = asyncio.run(_ConcreteProvider().count_tokens(msg))
        assert tokens > 4


class TestTokenize:
    def test_empty_returns_zero(self):
        assert asyncio.run(_ConcreteProvider()._tokenize("")) == 0

    def test_english_text(self):
        tokens = asyncio.run(_ConcreteProvider()._tokenize("hello world"))
        assert 2 <= tokens <= 3

    def test_chinese_text(self):
        tokens = asyncio.run(_ConcreteProvider()._tokenize("你好世界"))
        assert tokens > 0

    def test_mixed_text(self):
        tokens = asyncio.run(_ConcreteProvider()._tokenize("hello 你好"))
        assert tokens > 0


from core.llm.provider import LLMProvider


class _ConcreteProvider(LLMProvider):
    """Minimal concrete LLMProvider for testing base class token counting."""

    @property
    def base_url(self) -> str:
        return "http://test"

    async def chat(self, messages, *, model, **kwargs):
        raise NotImplementedError

    def chat_stream(self, messages, *, model, **kwargs):
        raise NotImplementedError
