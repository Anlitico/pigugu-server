# tests/unit/core/agent/test_runner.py
"""Tests for core.agent.runner  -  AgentRunner, RunnerConfig."""

import asyncio

from core.agent.runner import AgentRunner, RunnerConfig
from core.agent.stop import StepResult
from core.agent.state import StateStatus


# ── Helpers ────────────────────────────────────────────────────────────────


async def _noop(*args, **kwargs):
    pass


def _make_load(messages: list):
    """Create an async on_before_step hook returning the given messages."""
    async def _load():
        return list(messages)
    return _load


class MockProvider:
    """Mock LLM provider that returns deterministic responses."""

    def __init__(self, responses: list[tuple[str, list | None]]):
        self._responses = responses
        self._call_count = 0

    async def chat(self, *, messages, model, tools, temperature, max_tokens):
        idx = min(self._call_count, len(self._responses) - 1)
        content, tool_calls = self._responses[idx]
        self._call_count += 1

        from core.llm.types import ChatResponse
        return ChatResponse(content=content, tool_calls=tool_calls)


class _FakeExecutor:
    async def run(self, tool_calls, timeout=None, concurrent=True):
        from core.agent.executor import ToolExecutionResult, ToolResult
        fake = [ToolResult(tool_call_id=tc.id, tool_name=tc.name, success=True, content="ok") for tc in tool_calls]
        return ToolExecutionResult(results=fake)


class MockMessage:
    """Minimal message-like object for test contexts."""

    def __init__(self, role, content):
        self.role = role
        self.content = content
        self.tool_calls = None
        self.tool_call_id = None
        self.name = None
        self.partial = False


# ── Tests ───────────────────────────────────────────────────────────────────


class TestRunnerConfig:
    def test_defaults(self):
        c = RunnerConfig()
        assert c.model == "qwen-plus-us"
        assert c.max_steps == 5


class TestAgentRunner:
    def test_single_step_no_tools(self, monkeypatch):
        provider = MockProvider([("Hello!", None)])
        monkeypatch.setattr("core.agent.runner.get_llm", lambda model: provider)

        runner = AgentRunner(RunnerConfig(model="test"))
        result = asyncio.run(runner.run(
            on_before_step=_make_load([MockMessage("user", "hi")]),
            on_after_step=_noop,
        ))
        assert runner.last_step_count == 1
        assert result.content == "Hello!"
        assert result.tool_calls is None
        assert runner.last_status == StateStatus.SUCCESS.value

    def test_multi_step_with_tools(self, monkeypatch):
        from core.llm.types import ToolCall
        tc = ToolCall(id="c1", name="search", arguments='{"q":"x"}')
        provider = MockProvider([
            ("let me search", [tc]),
            ("found it", None),
        ])
        monkeypatch.setattr("core.agent.runner.get_llm", lambda model: provider)

        runner = AgentRunner(RunnerConfig(
            model="test",
            tool_handlers={"search": lambda args: "result"},
        ))
        messages = []

        async def _flush(msgs, st):
            messages.extend(msgs)

        result = asyncio.run(runner.run(
            on_before_step=_make_load([MockMessage("user", "find x")]),
            on_after_step=_flush,
        ))
        assert runner.last_step_count == 2
        assert result.content == "found it"
        assert any(getattr(m, "role", "") == "tool" for m in messages)

    def test_stops_on_step_count(self, monkeypatch):
        from core.llm.types import ToolCall
        tc = ToolCall(id="c1", name="loop", arguments="{}")
        provider = MockProvider([
            ("step 1", [tc]),
            ("step 2", [tc]),
            ("step 3", None),
        ])
        monkeypatch.setattr("core.agent.runner.get_llm", lambda model: provider)
        monkeypatch.setattr("core.agent.runner.ToolExecutor", lambda *a, **kw: _FakeExecutor())

        runner = AgentRunner(RunnerConfig(model="test", max_steps=2))
        result = asyncio.run(runner.run(
            on_before_step=_make_load([MockMessage("user", "loop")]),
            on_after_step=_noop,
        ))
        assert runner.last_step_count == 2

    def test_status_on_error(self, monkeypatch):
        class FailingProvider:
            async def chat(self, **kw):
                raise RuntimeError("LLM down")

        monkeypatch.setattr("core.agent.runner.get_llm", lambda model: FailingProvider())

        runner = AgentRunner(RunnerConfig(model="test"))
        asyncio.run(runner.run(
            on_before_step=_make_load([MockMessage("user", "hi")]),
            on_after_step=_noop,
        ))
        assert runner.last_status == StateStatus.ERROR.value

    def test_on_after_step_called_on_error(self, monkeypatch):
        called = False

        class FailingProvider:
            async def chat(self, **kw):
                raise RuntimeError("down")

        monkeypatch.setattr("core.agent.runner.get_llm", lambda model: FailingProvider())

        async def _flush(msgs, st):
            nonlocal called
            called = True

        runner = AgentRunner(RunnerConfig(model="test"))
        asyncio.run(runner.run(
            on_before_step=_make_load([MockMessage("user", "hi")]),
            on_after_step=_flush,
        ))
        assert called

    def test_interrupt_key_calls_guarded(self, monkeypatch):
        from core.agent.interrupt import get_interrupt_manager

        class SlowProvider:
            async def chat(self, **kw):
                await asyncio.sleep(0.1)
                from core.llm.types import ChatResponse
                return ChatResponse(content="slow", tool_calls=None)

        monkeypatch.setattr("core.agent.runner.get_llm", lambda model: SlowProvider())

        runner = AgentRunner(RunnerConfig(model="test", interrupt_key="test:guard"))

        async def _run():
            mgr = get_interrupt_manager()
            task = asyncio.create_task(runner.run(
                on_before_step=_make_load([MockMessage("user", "hi")]),
                on_after_step=_noop,
            ))
            await asyncio.sleep(0.02)
            await mgr.trigger("test:guard")
            return await task

        result = asyncio.run(_run())
        assert runner.last_status == StateStatus.INTERRUPTED.value
        assert result.finish_reason == "interrupted"
