# tests/unit/core/agent/test_stop.py
"""Tests for core.agent.stop — StepResult, step_count_is, no_tool_calls."""


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

        class FakeRunner:
            current_step = 3
        assert not step_count_is(5)(FakeRunner)

    def test_step_count_is_at_limit(self):
        from core.agent.stop import step_count_is

        class FakeRunner:
            current_step = 5
        assert step_count_is(5)(FakeRunner)

    def test_step_count_is_above(self):
        from core.agent.stop import step_count_is

        class FakeRunner:
            current_step = 10
        assert step_count_is(5)(FakeRunner)

    def test_no_tool_calls_last_result_none(self):
        from core.agent.stop import no_tool_calls

        class FakeRunner:
            last_result = None
        assert not no_tool_calls(FakeRunner)

    def test_no_tool_calls_no_tools(self):
        from core.agent.stop import no_tool_calls, StepResult

        class FakeRunner:
            last_result = StepResult()
        assert no_tool_calls(FakeRunner)

    def test_no_tool_calls_with_tools(self):
        from core.agent.stop import no_tool_calls, StepResult

        class FakeRunner:
            last_result = StepResult(tool_calls=[None])
        assert not no_tool_calls(FakeRunner)
