# tests/unit/core/agent/test_stop.py
"""Tests for core.agent.stop — StepResult, step_count_is, no_tool_calls."""

from core.agent.state import AgentState


class TestStepResult:
    def test_defaults(self):
        from core.agent.stop import StepResult
        r = StepResult()
        assert r.messages == []
        assert r.content == ""
        assert r.tool_calls is None
        assert r.finish_reason == ""

    def test_with_tool_calls(self):
        from core.llm.types import ToolCall
        from core.agent.stop import StepResult
        tc = ToolCall(id="1", name="test", arguments="{}")
        r = StepResult(tool_calls=[tc], finish_reason="tool_calls", content="let me check")
        assert r.tool_calls is not None
        assert len(r.tool_calls) == 1
        assert r.content == "let me check"


class TestStopConditions:
    def test_step_count_is_below(self):
        from core.agent.stop import step_count_is
        state = AgentState(current_step=3)
        assert not step_count_is(5)(state)

    def test_step_count_is_at_limit(self):
        from core.agent.stop import step_count_is
        state = AgentState(current_step=5)
        assert step_count_is(5)(state)

    def test_step_count_is_above(self):
        from core.agent.stop import step_count_is
        state = AgentState(current_step=10)
        assert step_count_is(5)(state)

    def test_no_tool_calls_last_result_none(self):
        """current_step=0 means no steps run yet — should not stop."""
        from core.agent.stop import no_tool_calls
        state = AgentState(current_step=0, last_had_tool_calls=False)
        assert not no_tool_calls(state)

    def test_no_tool_calls_no_tools(self):
        from core.agent.stop import no_tool_calls
        state = AgentState(current_step=1, last_had_tool_calls=False)
        assert no_tool_calls(state)

    def test_no_tool_calls_with_tools(self):
        from core.agent.stop import no_tool_calls
        state = AgentState(current_step=1, last_had_tool_calls=True)
        assert not no_tool_calls(state)
