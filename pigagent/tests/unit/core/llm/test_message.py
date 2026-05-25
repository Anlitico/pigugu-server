# tests/unit/core/llm/test_message.py
"""Tests for Message serialization  -  to_dict/from_dict roundtrip and edge cases."""

from core.llm.types import Message, ToolCall


class TestMessageRoundtrip:
    def test_simple_roundtrip(self):
        m = Message(role="user", content="hello")
        restored = Message.from_dict(m.to_dict())
        assert restored.role == "user"
        assert restored.content == "hello"

    def test_with_partial(self):
        m = Message(role="assistant", content="cont...", partial=True)
        restored = Message.from_dict(m.to_dict())
        assert restored.partial is True

    def test_with_tool_calls(self):
        tc = ToolCall(id="c1", name="search", arguments='{"q":"test"}')
        m = Message(role="assistant", content="", tool_calls=[tc])
        restored = Message.from_dict(m.to_dict())
        assert restored.tool_calls is not None
        assert restored.tool_calls[0].id == "c1"
        assert restored.tool_calls[0].name == "search"
        assert restored.tool_calls[0].arguments == '{"q":"test"}'

    def test_with_tool_call_id(self):
        m = Message(role="tool", content="result", tool_call_id="c1", name="search")
        restored = Message.from_dict(m.to_dict())
        assert restored.tool_call_id == "c1"
        assert restored.name == "search"

    def test_extra_keys_ignored(self):
        """from_dict ignores unknown keys."""
        d = {"turn": 42, "role": "user", "content": "hi"}
        m = Message.from_dict(d)
        assert m.role == "user"
        assert not hasattr(m, "turn")

    def test_missing_optional_fields(self):
        """Fields not in the dict get defaults."""
        d = {"role": "user", "content": "hi"}
        m = Message.from_dict(d)
        assert m.tool_calls is None
        assert m.tool_call_id is None
        assert m.name is None
        assert m.partial is False

    def test_multiple_tool_calls_roundtrip(self):
        tc1 = ToolCall(id="1", name="a", arguments="{}")
        tc2 = ToolCall(id="2", name="b", arguments='{"k":"v"}')
        m = Message(role="assistant", content="", tool_calls=[tc1, tc2])
        restored = Message.from_dict(m.to_dict())
        assert restored.tool_calls is not None
        assert len(restored.tool_calls) == 2
        assert restored.tool_calls[0].id == "1"
        assert restored.tool_calls[1].arguments == '{"k":"v"}'

    def test_to_dict_full(self):
        """Verify to_dict includes all set fields."""
        tc = ToolCall(id="c1", name="s", arguments="{}")
        m = Message(role="assistant", content="text", partial=True,
                    tool_calls=[tc], tool_call_id="tid", name="nm")
        d = m.to_dict()
        assert d["role"] == "assistant"
        assert d["partial"] is True
        assert "tool_calls" in d
        assert d["tool_call_id"] == "tid"
        assert d["name"] == "nm"
