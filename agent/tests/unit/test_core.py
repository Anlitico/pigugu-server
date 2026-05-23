# tests/unit/test_core.py
"""Unit tests for core agent infrastructure — state, types, interrupt."""

import asyncio

import pytest

from core.agent.stop import StepResult, step_count_is, no_tool_calls
from core.agent.state import AgentState, StateStatus
from core.agent.interrupt import InterruptManager, get_interrupt_manager, check_interrupt


class TestStepResult:
    def test_defaults(self):
        r = StepResult()
        assert r.messages == []
        assert r.tool_calls is None
        assert r.finish_reason == ""

    def test_with_tool_calls(self):
        from core.llm.types import ToolCall
        tc = ToolCall(id="1", name="test", arguments="{}")
        r = StepResult(tool_calls=[tc], finish_reason="tool_calls")
        assert len(r.tool_calls) == 1


class TestStopConditions:
    def test_step_count_is(self):
        class FakeRunner:
            current_step = 5
        cond = step_count_is(5)

        FakeRunner.current_step = 4
        assert not cond(FakeRunner)
        FakeRunner.current_step = 5
        assert cond(FakeRunner)
        FakeRunner.current_step = 6
        assert cond(FakeRunner)

    def test_no_tool_calls(self):
        class FakeRunner:
            last_result = None
        r = FakeRunner()
        assert not no_tool_calls(r)
        r.last_result = StepResult()
        assert no_tool_calls(r)
        r.last_result = StepResult(tool_calls=[None])
        assert not no_tool_calls(r)





class TestAgentState:
    def test_defaults(self):
        s = AgentState()
        assert s.status == StateStatus.RUNNING.value
        assert s.is_running
        assert not s.is_terminal

    def test_terminal_states(self):
        for status in ["success", "fail", "error", "interrupted"]:
            s = AgentState(status=status)
            assert s.is_terminal

    def test_roundtrip(self):
        s = AgentState(status="running")
        assert s.status == "running"
        assert s.is_running
        assert not s.is_terminal


class TestInterruptManager:
    def test_create_and_get(self):
        mgr = InterruptManager()
        event = mgr.create("test:1")
        assert mgr.get("test:1") is event
        assert not event.is_set()

    def test_trigger(self):
        mgr = InterruptManager()
        mgr.create("test:2")

        async def _run():
            await mgr.trigger("test:2")

        asyncio.run(_run())
        assert mgr.is_set("test:2")

    def test_cleanup(self):
        mgr = InterruptManager()
        mgr.create("test:3")
        mgr.cleanup("test:3")
        assert mgr.get("test:3") is None

    def test_recreate_replaces(self):
        mgr = InterruptManager()
        e1 = mgr.create("test:4")
        e2 = mgr.create("test:4")
        assert e1 is not e2
        assert mgr.get("test:4") is e2

    def test_get_stats(self):
        mgr = InterruptManager()
        mgr.create("test:5")
        stats = mgr.get_stats()
        assert stats["total_events"] == 1


class TestCheckInterrupt:
    def test_no_key_executes_normally(self):
        called = False

        @check_interrupt(key="")
        async def func(state: AgentState):
            nonlocal called
            called = True
            return "done"

        state = AgentState()
        result = asyncio.run(func(state=state))
        assert called
        assert result == "done"

    def test_with_key_no_interrupt(self):
        @check_interrupt(key="test:normal")
        async def func(state: AgentState):
            return "ok"

        state = AgentState()
        result = asyncio.run(func(state=state))
        assert result == "ok"

    def test_interrupt_cancels_func(self):
        @check_interrupt(key="test:cancel")
        async def slow_func(state: AgentState):
            await asyncio.sleep(10)
            return "never"

        state = AgentState()

        async def _run():
            # Trigger interrupt after short delay
            async def _trigger():
                await asyncio.sleep(0.05)
                loop = asyncio.get_running_loop()
                # Use asyncio.Event directly
                mgr = get_interrupt_manager()
                await mgr.trigger("test:cancel")

            task = asyncio.create_task(_trigger())
            result = await slow_func(state=state)
            await task
            return result

        result = asyncio.run(_run())
        assert result is None
        assert state.status == StateStatus.INTERRUPTED.value
