# tests/unit/core/agent/test_interrupt.py
"""Tests for core.agent.interrupt  -  InterruptManager, check_interrupt."""

import asyncio


class TestInterruptManager:
    def test_create_and_get(self):
        from core.agent.interrupt import InterruptManager
        mgr = InterruptManager()
        event = mgr.create("test:1")
        assert mgr.get("test:1") is event
        assert not event.is_set()

    def test_trigger(self):
        from core.agent.interrupt import InterruptManager
        mgr = InterruptManager()
        mgr.create("test:2")
        asyncio.run(mgr.trigger("test:2"))
        assert mgr.is_set("test:2")

    def test_trigger_missing_auto_creates(self):
        from core.agent.interrupt import InterruptManager
        mgr = InterruptManager()
        asyncio.run(mgr.trigger("test:auto"))
        assert mgr.is_set("test:auto")

    def test_cleanup(self):
        from core.agent.interrupt import InterruptManager
        mgr = InterruptManager()
        mgr.create("test:3")
        mgr.cleanup("test:3")
        assert mgr.get("test:3") is None

    def test_cleanup_unknown_does_not_raise(self):
        from core.agent.interrupt import InterruptManager
        mgr = InterruptManager()
        mgr.cleanup("nonexistent")

    def test_recreate_replaces(self):
        from core.agent.interrupt import InterruptManager
        mgr = InterruptManager()
        e1 = mgr.create("test:4")
        e2 = mgr.create("test:4")
        assert e1 is not e2
        assert mgr.get("test:4") is e2
        assert e1.is_set()  # old event was awoken

    def test_get_stats(self):
        from core.agent.interrupt import InterruptManager
        mgr = InterruptManager()
        mgr.create("test:5")
        stats = mgr.get_stats()
        assert stats["total_events"] == 1

    def test_get_all_keys(self):
        from core.agent.interrupt import InterruptManager
        mgr = InterruptManager()
        mgr.create("a")
        mgr.create("b")
        assert set(mgr.get_all_keys()) == {"a", "b"}

    def test_get_interrupted_keys(self):
        from core.agent.interrupt import InterruptManager
        mgr = InterruptManager()
        mgr.create("a")
        mgr.create("b")
        asyncio.run(mgr.trigger("a"))
        assert mgr.get_interrupted_keys() == ["a"]

    def test_is_set_false_for_unknown(self):
        from core.agent.interrupt import InterruptManager
        assert not InterruptManager().is_set("nope")


class TestCheckInterrupt:
    def test_no_key_executes_normally(self):
        from core.agent.interrupt import check_interrupt
        from core.agent.state import AgentState

        @check_interrupt(key="")
        async def func(state: AgentState):
            return "done"

        state = AgentState()
        result = asyncio.run(func(state=state))
        assert result == "done"

    def test_with_key_no_interrupt(self):
        from core.agent.interrupt import check_interrupt
        from core.agent.state import AgentState

        @check_interrupt(key="test:normal")
        async def func(state: AgentState):
            return "ok"

        state = AgentState()
        result = asyncio.run(func(state=state))
        assert result == "ok"

    def test_interrupt_cancels_func(self):
        from core.agent.interrupt import check_interrupt, get_interrupt_manager
        from core.agent.state import AgentState, StateStatus

        @check_interrupt(key="test:cancel")
        async def slow_func(state: AgentState):
            await asyncio.sleep(10)
            return "never"

        state = AgentState()

        async def _run():
            mgr = get_interrupt_manager()
            task = asyncio.create_task(slow_func(state=state))
            await asyncio.sleep(0.05)
            await mgr.trigger("test:cancel")
            result = await task
            return result

        result = asyncio.run(_run())
        assert result is None
        assert state.status == StateStatus.INTERRUPTED.value

    def test_skips_when_already_interrupted(self):
        from core.agent.interrupt import check_interrupt
        from core.agent.state import AgentState, StateStatus

        called = False

        @check_interrupt(key="test:skip", skip_on_interrupted=True)
        async def func(state: AgentState):
            nonlocal called
            called = True
            return "ran"

        state = AgentState(status=StateStatus.INTERRUPTED.value)
        result = asyncio.run(func(state=state))
        assert result is None
        assert not called

    def test_on_success_callback(self):
        from core.agent.interrupt import check_interrupt
        from core.agent.state import AgentState
        from core.agent.interrupt import InterruptedException  # noqa: F811

        results = []

        def _on_success(state, result):
            results.append(("success", result))

        @check_interrupt(key="test:cb", on_success=_on_success)
        async def func(state: AgentState):
            return "hello"

        state = AgentState()
        result = asyncio.run(func(state=state))
        assert result == "hello"
        assert results == [("success", "hello")]

    def test_on_error_callback(self):
        from core.agent.interrupt import check_interrupt
        from core.agent.state import AgentState

        errors = []

        def _on_error(state, exception):
            errors.append(str(exception))

        @check_interrupt(key="test:err", on_error=_on_error)
        async def func(state: AgentState):
            raise ValueError("boom")

        state = AgentState()
        result = asyncio.run(func(state=state))
        assert result is None
        assert "boom" in errors[0]
        assert state.status == "error"
