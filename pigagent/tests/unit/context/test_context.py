# tests/unit/context/test_context.py
"""Unit tests for core types and validation  -  Message serialization, tool call
validation, token counting, context config constants."""

import pytest

from core.llm.types import Message, ToolCall
from context.schema import WorkingContext
from core.agent.sanitize import validate_tool_calls
from config import get_config

_cfg = get_config()


# -------------------------------------------------------------------------------
# Token counting
# -------------------------------------------------------------------------------

class TestProviderTokenCounting:
    """Token counting: fast (tiktoken) + async (API, falls back to offline)."""

    def test_qwen_count_tokens_uses_tiktoken(self):
        from core.llm.providers.qwen import QwenProvider
        import asyncio
        p = QwenProvider(api_key="sk-test")

        async def run():
            assert await p.count_tokens("") == 0
            tokens = await p.count_tokens("hello world")
            assert 2 <= tokens <= 3
        asyncio.run(run())

    def test_qwen_count_tokens_chinese(self):
        from core.llm.providers.qwen import QwenProvider
        import asyncio
        p = QwenProvider(api_key="sk-test")

        async def run():
            tokens = await p.count_tokens("你好世界")
            assert tokens > 0
        asyncio.run(run())

    def test_volcengine_count_tokens_uses_tiktoken(self):
        from core.llm.providers.volcengine import VolcengineProvider
        import asyncio
        p = VolcengineProvider(api_key="sk-test")

        async def run():
            assert await p.count_tokens("") == 0
            tokens = await p.count_tokens("hello world")
            assert 2 <= tokens <= 3
        asyncio.run(run())

    def test_to_messages_uses_token_counter(self):
        """to_messages() should use passed token_counter for budget tracking."""
        wc = WorkingContext(user_id="u1")
        wc.summary = "test summary"
        msgs = wc.to_messages(
            token_counter=lambda t: 100 if t else 0,
        )
        assert wc.budget.layer_3_session == 100
        assert len(msgs) == 1


# -------------------------------------------------------------------------------
# Context config constants
# -------------------------------------------------------------------------------

class TestConstants:
    def test_hot_window_size(self):
        assert _cfg.CONTEXT_HOT_WINDOW_SIZE == 500

    def test_max_turns(self):
        assert _cfg.CONTEXT_MAX_TURNS == 400

    def test_token_budget_cap(self):
        assert _cfg.CONTEXT_TOKEN_BUDGET_CAP == 200_000

    def test_roast_compression_ratio(self):
        assert _cfg.CONTEXT_ROAST_COMPRESSION_RATIO == 0.05

    def test_roast_compression_min_tokens(self):
        assert _cfg.CONTEXT_ROAST_COMPRESSION_MIN_TOKENS == 1000

    def test_l3_compress_max_words(self):
        assert _cfg.CONTEXT_L3_COMPRESS_MAX_WORDS == 5000

    def test_l3_merge_max_words(self):
        assert _cfg.CONTEXT_L3_MERGE_MAX_WORDS == 8000

    def test_l4_roast_max_words(self):
        assert _cfg.CONTEXT_L4_ROAST_MAX_WORDS == 5000

    def test_l2_profile_max_words(self):
        assert _cfg.CONTEXT_L2_PROFILE_MAX_WORDS == 1500


# -------------------------------------------------------------------------------
# Message serialization
# -------------------------------------------------------------------------------

class TestMessageSerialization:
    """Verify Message.to_dict/from_dict roundtrip (used for Redis storage)."""

    def test_simple_roundtrip(self):
        m = Message(role="user", content="hello")
        d = m.to_dict()
        restored = Message.from_dict(d)
        assert restored.role == "user"
        assert restored.content == "hello"

    def test_with_partial(self):
        m = Message(role="assistant", content="cont...", partial=True)
        d = m.to_dict()
        restored = Message.from_dict(d)
        assert restored.partial is True

    def test_extra_keys_ignored(self):
        """turn_number in JSON wrapper should be ignored by from_dict."""
        d = {"turn": 42, "role": "user", "content": "hi"}
        m = Message.from_dict(d)
        assert m.role == "user"
        assert not hasattr(m, "turn")


# -------------------------------------------------------------------------------
# Tool call validation
# -------------------------------------------------------------------------------

class TestToolCallValidation:
    """validate_tool_calls  -  filter incomplete tool calls before LLM context."""

    def test_empty_list(self):
        assert validate_tool_calls([]) == []

    def test_no_tool_calls_passthrough(self):
        msgs = [Message.user("hello"), Message.assistant("hi")]
        result = validate_tool_calls(msgs)
        assert len(result) == 2

    def test_complete_tool_chain_passes(self):
        tc = ToolCall(id="call_1", name="get_weather", arguments='{"city":"BJ"}')
        msgs = [
            Message.user("what is the weather"),
            Message.assistant(tool_calls=[tc]),
            Message.tool(call_id="call_1", name="get_weather", content='{"temp":25}'),
            Message.assistant("It's 25 degrees."),
        ]
        result = validate_tool_calls(msgs)
        assert len(result) == 4
        assert result[1].tool_calls is not None

    def test_dangling_tool_call_removed(self):
        """Assistant with tool_calls but no matching response -> strip calls, keep text."""
        tc = ToolCall(id="call_1", name="get_weather", arguments='{}')
        msgs = [
            Message.user("weather?"),
            Message.assistant(content="Let me check.", tool_calls=[tc]),
        ]
        result = validate_tool_calls(msgs)
        assert len(result) == 2
        assert result[1].tool_calls is None
        assert result[1].content == "Let me check."

    def test_assistant_only_dangling_calls_dropped(self):
        """Assistant with only tool_calls and no content -> dropped entirely."""
        tc = ToolCall(id="call_1", name="get_weather", arguments='{}')
        msgs = [
            Message.user("weather?"),
            Message.assistant(tool_calls=[tc]),
            Message.user("nevermind"),
        ]
        result = validate_tool_calls(msgs)
        assert len(result) == 2
        assert result[0].role == "user"
        assert result[1].role == "user"

    def test_partial_fulfillment(self):
        """One call resolved, one not -> keep only resolved."""
        tc1 = ToolCall(id="call_1", name="get_weather", arguments='{}')
        tc2 = ToolCall(id="call_2", name="get_time", arguments='{}')
        msgs = [
            Message.user("weather and time?"),
            Message.assistant(tool_calls=[tc1, tc2]),
            Message.tool(call_id="call_1", name="get_weather", content='{"temp":25}'),
        ]
        result = validate_tool_calls(msgs)
        assert len(result[1].tool_calls) == 1
        assert result[1].tool_calls[0].id == "call_1"

    def test_tool_without_call_id_kept(self):
        msgs = [
            Message(role="tool", content="orphan result"),
        ]
        result = validate_tool_calls(msgs)
        assert len(result) == 1
