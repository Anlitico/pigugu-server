# tests/unit/core/agent/test_executor.py
"""Tests for core.agent.executor  -  ToolExecutor, ToolResult, ToolExecutionResult."""

import asyncio

import pytest

from core.agent.executor import ToolExecutor, ToolResult, ToolExecutionResult


class TestToolResult:
    def test_defaults(self):
        r = ToolResult(tool_call_id="1", tool_name="test", success=True)
        assert r.content == ""
        assert r.error is None

    def test_failure(self):
        r = ToolResult(tool_call_id="1", tool_name="test", success=False, error="boom")
        assert r.error == "boom"


class TestToolExecutionResult:
    def test_defaults(self):
        r = ToolExecutionResult()
        assert r.results == []
        assert r.all_success  # empty is all success

    def test_all_success_mixed(self):
        r = ToolExecutionResult(results=[
            ToolResult(tool_call_id="1", tool_name="a", success=True),
            ToolResult(tool_call_id="2", tool_name="b", success=False),
        ])
        assert not r.all_success


class TestToolExecutor:
    def test_empty_tool_calls(self):
        executor = ToolExecutor(handlers={})
        result = asyncio.run(executor.run([]))
        assert result.results == []

    def test_missing_handler(self):
        from core.llm.types import ToolCall
        executor = ToolExecutor(handlers={})
        tc = ToolCall(id="1", name="unknown", arguments="{}")
        results = asyncio.run(executor.run([tc]))
        assert not results.results[0].success
        assert results.results[0].error and "No handler" in results.results[0].error

    def test_invalid_json_arguments(self):
        from core.llm.types import ToolCall
        executor = ToolExecutor(handlers={"test": lambda args: "ok"})
        tc = ToolCall(id="1", name="test", arguments="not json")
        results = asyncio.run(executor.run([tc]))
        assert not results.results[0].success
        assert results.results[0].error and "Invalid JSON" in results.results[0].error

    def test_executes_handler(self):
        from core.llm.types import ToolCall

        def handler(args: dict) -> str:
            return f"got {args['key']}"

        executor = ToolExecutor(handlers={"test": handler})
        tc = ToolCall(id="1", name="test", arguments='{"key": "value"}')
        results = asyncio.run(executor.run([tc]))
        assert results.results[0].success
        assert "got value" in results.results[0].content

    def test_async_handler(self):
        from core.llm.types import ToolCall

        async def handler(args: dict) -> str:
            await asyncio.sleep(0.01)
            return "async result"

        executor = ToolExecutor(handlers={"test": handler})
        tc = ToolCall(id="1", name="test", arguments="{}")
        results = asyncio.run(executor.run([tc]))
        assert results.results[0].success
        assert results.results[0].content == "async result"

    def test_concurrent_execution(self):
        from core.llm.types import ToolCall
        order = []

        async def handler(args: dict) -> str:
            order.append(args["id"])
            await asyncio.sleep(0.02)
            return args["id"]

        executor = ToolExecutor(handlers={"t": handler})
        calls = [
            ToolCall(id="1", name="t", arguments='{"id": "a"}'),
            ToolCall(id="2", name="t", arguments='{"id": "b"}'),
            ToolCall(id="3", name="t", arguments='{"id": "c"}'),
        ]
        results = asyncio.run(executor.run(calls))
        assert results.all_success
        assert len(results.results) == 3

    def test_timeout(self):
        from core.llm.types import ToolCall

        async def handler(args: dict) -> str:
            await asyncio.sleep(10)
            return "never"

        executor = ToolExecutor(handlers={"slow": handler})
        tc = ToolCall(id="1", name="slow", arguments="{}")
        results = asyncio.run(executor.run([tc], timeout=0.05))
        assert not results.results[0].success
        assert results.results[0].error and "timed out" in results.results[0].error.lower()

    def test_handler_exception(self):
        from core.llm.types import ToolCall

        def handler(args: dict) -> str:
            raise ValueError("boom")

        executor = ToolExecutor(handlers={"fail": handler})
        tc = ToolCall(id="1", name="fail", arguments="{}")
        results = asyncio.run(executor.run([tc]))
        assert not results.results[0].success
        assert results.results[0].error and "boom" in results.results[0].error

    def test_dict_result_serialized(self):
        from core.llm.types import ToolCall

        def handler(args: dict) -> dict:
            return {"key": "value"}

        executor = ToolExecutor(handlers={"t": handler})
        tc = ToolCall(id="1", name="t", arguments="{}")
        results = asyncio.run(executor.run([tc]))
        assert results.results[0].success
        assert '"key"' in results.results[0].content

    def test_run_single(self):
        from core.llm.types import ToolCall

        executor = ToolExecutor(handlers={"t": lambda a: "ok"})
        tc = ToolCall(id="1", name="t", arguments="{}")
        result = asyncio.run(executor.run_single(tc))
        assert result.success
        assert result.content == "ok"

    def test_inject_extracted_from_handler_result(self):
        """_inject key is popped from content and stored in ToolResult.inject."""
        from core.llm.types import ToolCall

        def handler(args: dict) -> dict:
            return {
                "message": "done",
                "_inject": [{"role": "user", "content": "injected body"}],
            }

        executor = ToolExecutor(handlers={"t": handler})
        tc = ToolCall(id="1", name="t", arguments="{}")
        result = asyncio.run(executor.run_single(tc))

        assert result.success
        # _inject should NOT be in content
        assert "_inject" not in result.content
        assert '"message": "done"' in result.content
        # _inject should be in the inject field
        assert result.inject is not None
        assert len(result.inject) == 1
        assert result.inject[0]["role"] == "user"
        assert result.inject[0]["content"] == "injected body"

    def test_inject_none_when_not_present(self):
        from core.llm.types import ToolCall

        executor = ToolExecutor(handlers={"t": lambda a: "ok"})
        tc = ToolCall(id="1", name="t", arguments="{}")
        result = asyncio.run(executor.run_single(tc))

        assert result.inject is None

    def test_inject_not_extracted_for_string_result(self):
        from core.llm.types import ToolCall

        executor = ToolExecutor(handlers={"t": lambda a: "plain string"})
        tc = ToolCall(id="1", name="t", arguments="{}")
        result = asyncio.run(executor.run_single(tc))

        assert result.inject is None

    def test_interrupt_cancels_pending(self):
        """Verify that CancelledError cleans up child tasks."""
        from core.llm.types import ToolCall

        async def slow(args: dict) -> str:
            await asyncio.sleep(10)
            return "never"

        executor = ToolExecutor(handlers={"slow": slow})
        calls = [
            ToolCall(id="1", name="slow", arguments="{}"),
            ToolCall(id="2", name="slow", arguments="{}"),
        ]

        async def _run():
            task = asyncio.create_task(executor.run(calls))
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(_run())
        # No orphaned tasks, no hanging
