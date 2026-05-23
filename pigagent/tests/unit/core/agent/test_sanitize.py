# tests/unit/core/agent/test_sanitize.py
"""Tests for core.agent.sanitize — validate_tool_calls, _len_fallback."""


class TestLenFallback:
    def test_empty(self):
        from core.agent.sanitize import _len_fallback
        assert _len_fallback("") == 0

    def test_string(self):
        from core.agent.sanitize import _len_fallback
        assert _len_fallback("hello") == 5

    def test_none(self):
        from core.agent.sanitize import _len_fallback
        assert _len_fallback(None) == 0


class TestValidateToolCalls:
    def test_empty_list(self):
        from core.agent.sanitize import validate_tool_calls
        assert validate_tool_calls([]) == []

    def test_no_tool_calls_passthrough(self):
        from core.agent.sanitize import validate_tool_calls
        from core.llm.types import Message
        msgs = [Message.user("hello"), Message.assistant("hi")]
        result = validate_tool_calls(msgs)
        assert len(result) == 2

    def test_complete_tool_chain_passes(self):
        from core.agent.sanitize import validate_tool_calls
        from core.llm.types import Message, ToolCall
        tc = ToolCall(id="call_1", name="get_weather", arguments='{"city":"BJ"}')
        msgs = [
            Message.user("weather?"),
            Message.assistant(tool_calls=[tc]),
            Message.tool(call_id="call_1", name="get_weather", content='{"temp":25}'),
            Message.assistant("It's 25 degrees."),
        ]
        result = validate_tool_calls(msgs)
        assert len(result) == 4
        assert result[1].tool_calls is not None

    def test_dangling_tool_call_removed(self):
        from core.agent.sanitize import validate_tool_calls
        from core.llm.types import Message, ToolCall
        tc = ToolCall(id="call_1", name="get_weather", arguments="{}")
        msgs = [
            Message.user("weather?"),
            Message.assistant(content="Let me check.", tool_calls=[tc]),
        ]
        result = validate_tool_calls(msgs)
        assert len(result) == 2
        assert result[1].tool_calls is None
        assert result[1].content == "Let me check."

    def test_dangling_only_calls_dropped_entirely(self):
        from core.agent.sanitize import validate_tool_calls
        from core.llm.types import Message, ToolCall
        tc = ToolCall(id="call_1", name="get_weather", arguments="{}")
        msgs = [
            Message.user("weather?"),
            Message.assistant(tool_calls=[tc]),  # no content
            Message.user("nevermind"),
        ]
        result = validate_tool_calls(msgs)
        assert len(result) == 2
        assert result[0].role == "user"
        assert result[1].role == "user"

    def test_partial_fulfillment(self):
        from core.agent.sanitize import validate_tool_calls
        from core.llm.types import Message, ToolCall
        tc1 = ToolCall(id="call_1", name="a", arguments="{}")
        tc2 = ToolCall(id="call_2", name="b", arguments="{}")
        msgs = [
            Message.user("two things"),
            Message.assistant(tool_calls=[tc1, tc2]),
            Message.tool(call_id="call_1", name="a", content="ok"),
        ]
        result = validate_tool_calls(msgs)
        assert len(result[1].tool_calls) == 1
        assert result[1].tool_calls[0].id == "call_1"
