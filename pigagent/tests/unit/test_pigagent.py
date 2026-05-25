# tests/unit/test_pigagent.py
"""Unit tests for PigAgent — generate_reply, start_roast, stream, tools."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.llm.types import Message


# ── Helpers ────────────────────────────────────────────────────────────────


def _make_agent(**kwargs):
    """Create a PigAgent with all required dependencies mocked."""
    from pigagent import PigAgent

    ctx = MagicMock()
    ctx.load = AsyncMock(return_value=[])
    ctx.add_turn = AsyncMock()

    redis = MagicMock()
    pg_pool = MagicMock()

    # Mock tools so we don't need real API keys
    mock_web_search = MagicMock()
    mock_web_search.spec = MagicMock()
    mock_web_search.name = "web_search"
    mock_web_search.execute = AsyncMock(return_value={"content": "results"})

    mock_volume = MagicMock()
    mock_volume.spec = MagicMock()
    mock_volume.name = "volume_control"
    mock_volume.execute = AsyncMock()

    mock_registry = MagicMock()
    mock_registry.tools = [mock_web_search.spec, mock_volume.spec]
    mock_registry.tool_handlers = {
        "web_search": mock_web_search.execute,
        "volume_control": mock_volume.execute,
    }
    mock_registry.get = lambda name: {
        "web_search": mock_web_search,
        "volume_control": mock_volume,
    }.get(name)

    defaults = {
        "ctx": ctx,
        "redis": redis,
        "pg_pool": pg_pool,
        "prompts": {"trump": "You are Trump."},
        "game_modes": {},
        "tools": mock_registry.tools,
        "tool_handlers": mock_registry.tool_handlers,
    }
    defaults.update(kwargs)
    return PigAgent(**defaults), ctx, redis, pg_pool


def _mock_runner_stream(agent, responses=None):
    """Replace agent.runner.stream with a mock that yields responses."""
    if responses is None:
        responses = ["Hello!"]
    mock = AsyncMock()
    mock.last_step_count = 1
    mock.last_status = "success"

    async def _stream(messages, search=None, interrupt_key=None):
        for r in responses:
            yield r

    mock.stream = _stream
    agent.runner = mock
    return mock


# ── generate_reply ─────────────────────────────────────────────────────────


class TestGenerateReply:
    def test_empty_text_returns_immediately(self):
        agent, ctx, redis, pg = _make_agent()
        _mock_runner_stream(agent)

        async def _run():
            chunks = []
            async for t in agent.generate_reply("u1", ""):
                chunks.append(t)
            return chunks

        import asyncio
        result = asyncio.run(_run())
        assert result == []

    def test_basic_chat_loads_context(self):
        agent, ctx, redis, pg = _make_agent()
        _mock_runner_stream(agent, ["Hi there!"])

        ctx.load = AsyncMock(return_value=[
            Message.user("previous msg"),
            Message.assistant("previous reply"),
        ])

        import asyncio
        result = asyncio.run(_run_collect(agent.generate_reply("u1", "hello")))

        ctx.load.assert_called_once_with(user_id="u1")
        assert result == "Hi there!"

    def test_injects_system_prompt(self):
        agent, ctx, redis, pg = _make_agent(
            prompts={"default": "You are helpful."},
        )
        captured_messages = []

        async def _stream(messages, search=None, interrupt_key=None):
            captured_messages.extend(messages)
            yield "ok"

        mock = MagicMock()
        mock.stream = _stream
        mock.last_step_count = 1
        mock.last_status = "success"
        agent.runner = mock

        import asyncio
        asyncio.run(_run_collect(agent.generate_reply("u1", "hello", persona_id="default")))

        assert captured_messages[0].role == "system"
        assert captured_messages[0].content == "You are helpful."

    def test_persists_turn_after_stream(self):
        agent, ctx, redis, pg = _make_agent()
        _mock_runner_stream(agent, ["response text"])

        import asyncio
        result = asyncio.run(_run_collect(agent.generate_reply("u1", "hello")))

        assert ctx.add_turn.call_count == 2
        # First call: user message
        assert ctx.add_turn.call_args_list[0][1]["role"] == "user"
        assert ctx.add_turn.call_args_list[0][1]["content"] == "hello"
        # Second call: assistant response
        assert ctx.add_turn.call_args_list[1][1]["role"] == "assistant"
        assert ctx.add_turn.call_args_list[1][1]["content"] == "response text"

    def test_context_load_failure_does_not_block(self):
        agent, ctx, redis, pg = _make_agent()
        _mock_runner_stream(agent, ["ok"])

        ctx.load = AsyncMock(side_effect=RuntimeError("redis down"))

        import asyncio
        result = asyncio.run(_run_collect(agent.generate_reply("u1", "hello")))
        assert result == "ok"

    def test_routes_to_roast_when_active(self):
        import asyncio

        game_mode = MagicMock()
        game_mode.tick = AsyncMock()

        agent, ctx, redis, pg = _make_agent(
            game_modes={"roast_together": game_mode},
        )

        # Simulate active roast
        from roast.state import RoastState
        roast = RoastState.__new__(RoastState)
        roast.user_id = "u1"
        roast.persona_id = "trump"
        roast.roast_id = "r1"
        roast.mode = MagicMock()
        roast.mode.__str__ = MagicMock(return_value="roast_together")  # type: ignore[reportAttributeAccessIssue]
        roast.roast_instance_id = "inst-1"
        roast.phase = MagicMock()
        roast.turn_count = 0
        roast.extra = {}

        agent.get_active_roast = AsyncMock(return_value=roast)

        # Mock consume
        async def _consume(*args, **kwargs):
            return None

        with patch("roast.pending.consume", _consume):
            async def _stream(messages, search=None, interrupt_key=None):
                yield "roast reply"

            mock = MagicMock()
            mock.stream = _stream
            mock.last_step_count = 1
            mock.last_status = "success"
            agent.runner = mock

            result = asyncio.run(_run_collect(
                agent.generate_reply("u1", "hello")
            ))

        assert result == "roast reply"

    def test_skips_persistence_when_no_ctx(self):
        agent, ctx, redis, pg = _make_agent(ctx=None)
        _mock_runner_stream(agent, ["ok"])

        import asyncio
        asyncio.run(_run_collect(agent.generate_reply("u1", "hello")))
        # No persistence without ctx — should not crash


# ── stream ──────────────────────────────────────────────────────────────────


class TestStream:
    def test_basic_stream(self):
        agent, ctx, redis, pg = _make_agent()
        _mock_runner_stream(agent, ["a", "b", "c"])

        import asyncio
        result = asyncio.run(_run_collect(
            agent.stream([Message.user("hi")], persona_id="trump")
        ))
        assert result == "abc"

    def test_prepends_system_prompt(self):
        agent, ctx, redis, pg = _make_agent(
            prompts={"test": "SYSTEM"},
        )
        captured = []

        async def _stream(messages, search=None, interrupt_key=None):
            captured.extend(messages)
            yield "x"

        mock = MagicMock()
        mock.stream = _stream
        agent.runner = mock

        import asyncio
        asyncio.run(_run_collect(
            agent.stream([Message.user("hi")], persona_id="test")
        ))

        assert captured[0].role == "system"
        assert captured[0].content == "SYSTEM"

    def test_no_prompt_when_persona_unknown(self):
        agent, ctx, redis, pg = _make_agent(prompts={})
        _mock_runner_stream(agent, ["x"])

        import asyncio
        result = asyncio.run(_run_collect(
            agent.stream([Message.user("hi")], persona_id="nobody")
        ))
        assert result == "x"


# ── start_roast ─────────────────────────────────────────────────────────────


class TestStartRoast:
    def test_unknown_game_mode_returns_silently(self):
        agent, ctx, redis, pg = _make_agent()

        import asyncio

        async def _collect():
            chunks = []
            async for t in agent.start_roast(
                "u1", "trump", "r1", "unknown_mode", "prompt",
            ):
                chunks.append(t)
            return chunks

        result = asyncio.run(_collect())
        assert result == []

    def test_creates_session_and_streams_reply(self):
        import asyncio

        game_mode = MagicMock()
        game_mode.tick = AsyncMock()
        game_mode.init_extra = MagicMock(return_value={"key": "val"})
        game_mode.system_prompt_extension = "Game rules here"

        agent, ctx, redis, pg = _make_agent(
            game_modes={"roast_together": game_mode},
        )
        _mock_runner_stream(agent, ["Opening line!"])

        # Mock RoastState.start
        from roast.state import RoastState
        roast = RoastState.__new__(RoastState)
        roast.user_id = "u1"
        roast.roast_id = "r1"
        roast.roast_instance_id = "inst-1"
        roast.phase = MagicMock()
        roast.turn_count = 0
        roast.extra = {}
        roast.mode = MagicMock()
        roast.mode.__str__ = MagicMock(return_value="roast_together")  # type: ignore[reportAttributeAccessIssue]

        agent.get_active_roast = AsyncMock(return_value=None)

        async def _consume(*args, **kwargs):
            return None

        with patch("roast.state.RoastState.start", new=AsyncMock(return_value=roast)):
            with patch("roast.pending.consume", _consume):
                result = asyncio.run(_run_collect(
                    agent.start_roast("u1", "trump", "r1", "roast_together", "prompt")
                ))

        assert result == "Opening line!"
        # Roast body should be persisted
        assert ctx.add_turn.call_count >= 1
        first_call_content = ctx.add_turn.call_args_list[0][1]["content"]
        assert "News Context" in first_call_content
        assert "prompt" in first_call_content
        assert "Game Mode" in first_call_content
        assert "Game rules here" in first_call_content


# ── tools ───────────────────────────────────────────────────────────────────


class TestDefaultTools:
    def test_returns_valid_registry(self):
        from unittest.mock import patch, MagicMock
        from pigagent import PigAgent

        search_tool = MagicMock()
        search_tool.name = "web_search"
        search_tool.execute = lambda x: None

        vol_tool = MagicMock()
        vol_tool.name = "volume_control"
        vol_tool.execute = lambda x: None

        with patch("tools.create_web_search_tool", return_value=search_tool), \
             patch("tools.volume_tool", vol_tool), \
             patch("tools.search.TavilyProvider"):
            registry = PigAgent._create_default_tools()

        assert len(registry) == 2
        assert "web_search" in registry
        assert "volume_control" in registry

    def test_web_search_has_execute_handler(self):
        from unittest.mock import patch, MagicMock
        from pigagent import PigAgent

        search_tool = MagicMock()
        search_tool.name = "web_search"
        search_tool.execute = lambda x: None

        vol_tool = MagicMock()
        vol_tool.name = "volume_control"

        with patch("tools.create_web_search_tool", return_value=search_tool), \
             patch("tools.volume_tool", vol_tool), \
             patch("tools.search.TavilyProvider", MagicMock()):
            registry = PigAgent._create_default_tools()

        tool = registry.get("web_search")
        assert tool is not None
        assert callable(tool.execute)

    def test_volume_control_has_execute_handler(self):
        from unittest.mock import patch, MagicMock
        from pigagent import PigAgent

        search_tool = MagicMock()
        search_tool.name = "web_search"

        vol_tool = MagicMock()
        vol_tool.name = "volume_control"
        vol_tool.execute = lambda x: None

        with patch("tools.create_web_search_tool", return_value=search_tool), \
             patch("tools.volume_tool", vol_tool), \
             patch("tools.search.TavilyProvider", MagicMock()):
            registry = PigAgent._create_default_tools()

        tool = registry.get("volume_control")
        assert tool is not None
        assert callable(tool.execute)


class TestBuildRoastBody:
    def test_prompt_only(self):
        agent, ctx, redis, pg = _make_agent()
        game_mode = MagicMock()
        game_mode.system_prompt_extension = ""
        body = agent._build_roast_body(game_mode=game_mode, prompt="News text")
        assert "## News Context" in body
        assert "News text" in body
        assert "## Game Mode" not in body

    def test_extension_only(self):
        agent, ctx, redis, pg = _make_agent()
        game_mode = MagicMock()
        game_mode.system_prompt_extension = "Rules"
        body = agent._build_roast_body(game_mode=game_mode, prompt="")
        assert "## Game Mode" in body
        assert "Rules" in body
        assert "## News Context" not in body

    def test_both(self):
        agent, ctx, redis, pg = _make_agent()
        game_mode = MagicMock()
        game_mode.system_prompt_extension = "Rules"
        body = agent._build_roast_body(game_mode=game_mode, prompt="News")
        assert "## News Context" in body
        assert "## Game Mode" in body

    def test_empty(self):
        agent, ctx, redis, pg = _make_agent()
        game_mode = MagicMock()
        game_mode.system_prompt_extension = ""
        body = agent._build_roast_body(game_mode=game_mode, prompt="")
        assert body == ""


# ── Roast lifecycle ─────────────────────────────────────────────────────────


class TestRoastLifecycle:
    def test_get_active_roast_returns_none_on_failure(self):
        agent, ctx, redis, pg = _make_agent()
        redis.get = MagicMock(side_effect=RuntimeError("boom"))

        import asyncio
        result = asyncio.run(agent.get_active_roast("u1"))
        assert result is None

    def test_close_roast_no_active_session(self):
        agent, ctx, redis, pg = _make_agent()
        redis.get = AsyncMock(return_value=None)

        import asyncio
        asyncio.run(agent.close_roast("u1"))
        # Should not raise


# ── model property ──────────────────────────────────────────────────────────


class TestModelProperty:
    def test_returns_model(self):
        agent, ctx, redis, pg = _make_agent()
        assert agent.model == "qwen3.6-plus"


# ── Helpers ─────────────────────────────────────────────────────────────────


async def _run_collect(gen):
    chunks = []
    async for t in gen:
        chunks.append(t)
    return "".join(chunks)
