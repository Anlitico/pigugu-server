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
    def __init__(self, inject=None):
        self._inject = inject

    async def run(self, tool_calls, timeout=None, concurrent=True):
        from core.agent.executor import ToolExecutionResult, ToolResult
        fake = []
        for tc in tool_calls:
            tr = ToolResult(tool_call_id=tc.id, tool_name=tc.name, success=True,
                            content="ok", inject=self._inject)
            fake.append(tr)
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


# ── Stream interrupt tests ──────────────────────────────────────────────────


class SlowStreamProvider:
    """Yields text chunks one at a time with a configurable delay."""

    def __init__(self, chunks: list[str], delay: float = 0.01):
        self._chunks = chunks
        self._delay = delay

    async def chat_stream(self, **kw):
        from core.llm.types import ChatDelta
        for text in self._chunks:
            await asyncio.sleep(self._delay)
            yield ChatDelta(content=text)
        yield ChatDelta(finish_reason="stop")


class TestStreamInterrupt:
    @staticmethod
    def _msgs(*contents: str) -> list:
        from core.llm.types import Message
        return [Message.user(c) for c in contents]

    def test_no_interrupt_key_works_normally(self, monkeypatch):
        provider = SlowStreamProvider(["Hello, ", "world!"])
        monkeypatch.setattr("core.agent.runner.get_llm", lambda model: provider)

        runner = AgentRunner(RunnerConfig(model="test"))
        msgs = self._msgs("hi")

        async def _collect():
            chunks = []
            async for t in runner.stream(msgs):
                chunks.append(t)
            return "".join(chunks)

        result = asyncio.run(_collect())
        assert result == "Hello, world!"
        assert runner.last_status == StateStatus.SUCCESS.value

    def test_interrupt_mid_stream_saves_partial(self, monkeypatch):
        provider = SlowStreamProvider(["Hello, ", "world! ", "How are you?"], delay=0.02)
        monkeypatch.setattr("core.agent.runner.get_llm", lambda model: provider)

        runner = AgentRunner(RunnerConfig(model="test"))
        msgs = self._msgs("hi")
        event = asyncio.Event()

        async def _run():
            chunks = []
            async for t in runner.stream(msgs, interrupt_event=event):
                chunks.append(t)
                if "".join(chunks).startswith("Hello, "):
                    event.set()
            return "".join(chunks)

        result = asyncio.run(_run())
        assert result.startswith("Hello, ")
        assert runner.last_status == StateStatus.INTERRUPTED.value
        assert len(runner.last_messages) > 1
        last_msg = runner.last_messages[-1]
        assert last_msg.partial is True
        assert "Hello, " in last_msg.content

    def test_injected_message_appended_after_tool_result(self, monkeypatch):
        """When a tool result has inject, the injected message appears in last_messages."""
        from core.llm.types import ToolCall, ChatDelta

        inject_msg = {"role": "user", "content": "[System - Game Background]\ngame body"}
        call_count = [0]

        class StepProvider:
            async def chat_stream(self, **kw):
                call_count[0] += 1
                if call_count[0] == 1:
                    tc = ToolCall(id="c1", name="start_roast", arguments='{"roast_id":"r1"}')
                    yield ChatDelta(tool_calls=[tc], finish_reason="tool_calls")
                else:
                    yield ChatDelta(content="opening line", finish_reason="stop")

        monkeypatch.setattr("core.agent.runner.get_llm", lambda model: StepProvider())
        monkeypatch.setattr(
            "core.agent.runner.ToolExecutor",
            lambda *a, **kw: _FakeExecutor(inject=[inject_msg]),
        )

        runner = AgentRunner(RunnerConfig(model="test", max_steps=5))
        msgs = self._msgs("start the game")

        async def _collect():
            chunks = []
            async for t in runner.stream(msgs):
                chunks.append(t)
            return chunks

        result = asyncio.run(_collect())
        assert "".join(result) == "opening line"

        # The injected message is in last_messages after the tool result
        roles = [m.role for m in runner.last_messages if hasattr(m, "role")]
        assert "tool" in roles
        # Injected user message should be present
        user_contents = [m.content for m in runner.last_messages if m.role == "user"]
        injected_found = any(inject_msg["content"] in c for c in user_contents)
        assert injected_found

    def test_interrupt_before_any_output(self, monkeypatch):
        provider = SlowStreamProvider(["a", "b", "c"], delay=0.05)
        monkeypatch.setattr("core.agent.runner.get_llm", lambda model: provider)

        runner = AgentRunner(RunnerConfig(model="test"))
        msgs = self._msgs("hi")
        event = asyncio.Event()

        async def _run():
            event.set()
            chunks = []
            async for t in runner.stream(msgs, interrupt_event=event):
                chunks.append(t)
            return "".join(chunks)

        result = asyncio.run(_run())
        assert result == ""
        assert runner.last_status == StateStatus.INTERRUPTED.value

    def test_interrupt_respected_between_steps(self, monkeypatch):
        from core.llm.types import ToolCall, ChatDelta

        step_counter = [0]

        def _make_provider():
            class StepProvider:
                async def chat_stream(self, **kw):
                    nonlocal step_counter
                    step_counter[0] += 1
                    if step_counter[0] == 1:
                        tc = ToolCall(id="c1", name="search", arguments='{"q":"x"}')
                        yield ChatDelta(tool_calls=[tc], finish_reason="tool_calls")
                    else:
                        yield ChatDelta(content="result text")
                        yield ChatDelta(finish_reason="stop")
            return StepProvider()

        monkeypatch.setattr("core.agent.runner.get_llm", lambda model: _make_provider())
        monkeypatch.setattr(
            "core.agent.runner.ToolExecutor",
            lambda *a, **kw: _FakeExecutor(),
        )

        runner = AgentRunner(RunnerConfig(model="test", max_steps=5))
        msgs = self._msgs("hi")
        event = asyncio.Event()

        async def _run():
            chunks = []
            async for t in runner.stream(msgs, interrupt_event=event):
                chunks.append(t)
                event.set()
            return "".join(chunks)

        result = asyncio.run(_run())
        assert runner.last_status == StateStatus.INTERRUPTED.value
        assert runner.last_step_count >= 1


# ── user_reply helpers ──────────────────────────────────────────────────────


class TestUserReply:
    def _make_tc(self, arguments: str):
        from core.llm.types import ToolCall
        return ToolCall(id="1", name="test", arguments=arguments, index=0)

    def test_pull_complete_field(self):
        from core.agent.runner import _pull_user_reply
        tc = self._make_tc('{"user_reply": "Let me check that for you", "q": "x"}')
        text = _pull_user_reply(tc)
        assert text == "Let me check that for you"

    def test_pull_streaming_partial_not_yet(self):
        from core.agent.runner import _pull_user_reply
        tc = self._make_tc('{"user_reply": "Let me check')  # no closing quote yet
        text = _pull_user_reply(tc)
        assert text is None

    def test_pull_no_user_reply(self):
        from core.agent.runner import _pull_user_reply
        tc = self._make_tc('{"q": "x"}')
        assert _pull_user_reply(tc) is None

    def test_pull_empty_args(self):
        from core.agent.runner import _pull_user_reply
        tc = self._make_tc("")
        assert _pull_user_reply(tc) is None

    def test_pull_escaped_quotes(self):
        from core.agent.runner import _pull_user_reply
        tc = self._make_tc('{"user_reply": "he said \\"hello\\"", "q": "x"}')
        text = _pull_user_reply(tc)
        assert text == 'he said "hello"'

    def test_user_reply_passed_through_to_handler(self, monkeypatch):
        """user_reply is yielded, tool handler receives user_reply in args (pass-through)."""
        from core.llm.types import ToolCall, ChatDelta
        from core.agent.runner import AgentRunner, RunnerConfig

        handler_calls = []
        call_count = [0]

        class LogExecutor:
            async def run(self, tool_calls, **kw):
                from core.agent.executor import ToolExecutionResult, ToolResult
                for tc in tool_calls:
                    import json
                    handler_calls.append(json.loads(tc.arguments))
                return ToolExecutionResult(results=[
                    ToolResult(tool_call_id=tc.id, tool_name=tc.name, success=True, content="ok")
                    for tc in tool_calls
                ])

        class StepProvider:
            async def chat_stream(self, **kw):
                call_count[0] += 1
                if call_count[0] == 1:
                    tc = ToolCall(id="c1", name="start_roast",
                                  arguments='{"user_reply": "Give me a moment, starting the game...", "roast_id": "r1"}',
                                  index=0)
                    yield ChatDelta(tool_calls=[tc], finish_reason="tool_calls")
                else:
                    yield ChatDelta(content="opening line", finish_reason="stop")

        monkeypatch.setattr("core.agent.runner.get_llm", lambda model: StepProvider())
        monkeypatch.setattr("core.agent.runner.ToolExecutor",
                            lambda *a, **kw: LogExecutor())

        runner = AgentRunner(RunnerConfig(model="test", max_steps=3))
        msgs = self._msgs("start the game")

        async def _collect():
            chunks = []
            async for t in runner.stream(msgs):
                chunks.append(t)
            return chunks

        result = asyncio.run(_collect())
        full = "".join(result)
        # user_reply text was yielded
        assert "Give me a moment" in full
        # Tool executor received user_reply in args (pass-through)
        assert len(handler_calls) == 1
        assert handler_calls[0]["roast_id"] == "r1"
        assert "starting the game" in handler_calls[0]["user_reply"]

    @staticmethod
    def _msgs(*contents: str) -> list:
        from core.llm.types import Message
        return [Message.user(c) for c in contents]
