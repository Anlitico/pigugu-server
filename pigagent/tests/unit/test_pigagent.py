# tests/unit/test_pigagent.py
"""Unit tests for PigAgent  -  generate_reply, start_roast, stream, tools."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.llm.types import Message


# ── Helpers ────────────────────────────────────────────────────────────────


def _make_agent(**kwargs):
    """Create a PigAgent with all required dependencies mocked."""
    from agent import PigAgent

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
        "prompts": {1: "You are Trump."},
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
    mock = MagicMock()
    mock.last_step_count = 1
    mock.last_status = "success"
    mock.last_messages = []

    async def _stream(messages, search=None, interrupt_event=None, session_id=None):
        # Simulate the runner appending assistant response to messages
        full = list(messages)
        for r in responses:
            full.append(Message.assistant(r))
            yield r
        mock.last_messages = full

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
            prompts={0: "You are helpful."},
        )
        captured_messages = []

        async def _stream(messages, search=None, interrupt_event=None, session_id=None):
            captured_messages.extend(messages)
            yield "ok"

        mock = MagicMock()
        mock.stream = _stream
        mock.last_step_count = 1
        mock.last_status = "success"
        agent.runner = mock

        import asyncio
        asyncio.run(_run_collect(agent.generate_reply("u1", "hello", persona_id=0)))

        assert captured_messages[0].role == "system"
        assert captured_messages[0].content == "You are helpful."

    def test_persists_turn_after_stream(self):
        agent, ctx, redis, pg = _make_agent()
        _mock_runner_stream(agent, ["response text"])

        import asyncio
        result = asyncio.run(_run_collect(agent.generate_reply("u1", "hello")))

        assert ctx.add_turn.call_count >= 2
        calls = ctx.add_turn.call_args_list
        # Find user message
        user_calls = [c for c in calls if c[1].get("role") == "user"]
        assistant_calls = [c for c in calls if c[1].get("role") == "assistant"]
        assert len(user_calls) >= 1
        assert any(c[1].get("content") == "hello" for c in user_calls)
        assert len(assistant_calls) >= 1
        assert any(c[1].get("content") == "response text" for c in assistant_calls)

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
        roast.persona_id = 1
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
            async def _stream(messages, search=None, interrupt_event=None, session_id=None):
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
        # No persistence without ctx  -  should not crash


# ── stream ──────────────────────────────────────────────────────────────────


class TestStream:
    def test_basic_stream(self):
        agent, ctx, redis, pg = _make_agent()
        _mock_runner_stream(agent, ["a", "b", "c"])

        import asyncio
        result = asyncio.run(_run_collect(
            agent.stream([Message.user("hi")], persona_id=1)
        ))
        assert result == "abc"

    def test_prepends_system_prompt(self):
        agent, ctx, redis, pg = _make_agent(
            prompts={0: "SYSTEM"},
        )
        captured = []

        async def _stream(messages, search=None, interrupt_event=None, session_id=None):
            captured.extend(messages)
            yield "x"

        mock = MagicMock()
        mock.stream = _stream
        agent.runner = mock

        import asyncio
        asyncio.run(_run_collect(
            agent.stream([Message.user("hi")], persona_id=0)
        ))

        assert captured[0].role == "system"
        assert captured[0].content == "SYSTEM"

    def test_no_prompt_when_persona_unknown(self):
        agent, ctx, redis, pg = _make_agent(prompts={})
        _mock_runner_stream(agent, ["x"])

        import asyncio
        result = asyncio.run(_run_collect(
            agent.stream([Message.user("hi")], persona_id=999)
        ))
        assert result == "x"


# ── start_roast ─────────────────────────────────────────────────────────────


class TestStartRoast:
    def test_unknown_game_mode_falls_back_to_roast_together(self):
        agent, ctx, redis, pg = _make_agent()
        _mock_runner_stream(agent, ["Opening line!"])

        import asyncio

        async def _collect():
            chunks = []
            async for t in agent.start_roast(
                "u1", 1, "r1", "unknown_mode", "prompt",
            ):
                chunks.append(t)
            return chunks

        result = asyncio.run(_collect())
        # activate_roast() uses GameModeRegistry.get() which falls back
        # to roast_together for unknown modes, so the roast starts normally.
        assert result == ["Opening line!"]

    def test_creates_session_and_streams_reply(self):
        import asyncio

        game_mode = MagicMock()
        game_mode.mode = MagicMock()
        game_mode.mode.__str__ = MagicMock(return_value="roast_together")
        game_mode.tick = AsyncMock()
        game_mode.init_extra = MagicMock(return_value={"key": "val"})
        game_mode.system_prompt_extension = "Game rules here"

        agent, ctx, redis, pg = _make_agent()
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
        roast.mode.__str__ = MagicMock(return_value="roast_together")

        agent.get_active_roast = AsyncMock(return_value=None)

        with (
            patch("roast.state.RoastState.start", new=AsyncMock(return_value=roast)),
            patch("roast.activate.GameModeRegistry") as mock_registry,
        ):
            mock_registry.get.return_value = game_mode

            result = asyncio.run(_run_collect(
                agent.start_roast("u1", 1, "r1", "roast_together", "prompt")
            ))

        assert result == "Opening line!"
        assert ctx.add_turn.call_count >= 1
        first_call_kwargs = ctx.add_turn.call_args_list[0][1]
        assert first_call_kwargs["role"] == "system"
        first_call_content = first_call_kwargs["content"]
        assert "News Context" in first_call_content
        assert "prompt" in first_call_content
        assert "Game Mode" in first_call_content
        assert "Game rules here" in first_call_content


# ── tools ───────────────────────────────────────────────────────────────────


class TestDefaultTools:
    def test_returns_valid_registry(self):
        from unittest.mock import patch, MagicMock
        from agent import PigAgent

        search_tool = MagicMock()
        search_tool.name = "web_search"
        search_tool.execute = lambda x: None

        vol_tool = MagicMock()
        vol_tool.name = "volume_control"
        vol_tool.execute = lambda x: None

        # _create_default_tools is now an instance method that reads self._pg_pool.
        # Pass empty tools to skip default tool creation in __init__, then
        # call _create_default_tools() inside the patch context.
        agent = PigAgent(pg_pool=None, redis=MagicMock(), ctx=None, tools=[], tool_handlers={})

        with patch("tools.create_web_search_tool", return_value=search_tool), \
             patch("tools.volume_tool", vol_tool), \
             patch("tools.search.TavilyProvider"):
            registry = agent._create_default_tools()

        assert len(registry) == 3
        assert "web_search" in registry
        assert "volume_control" in registry
        assert "mark_roast_complete" in registry

    def test_web_search_has_execute_handler(self):
        from unittest.mock import patch, MagicMock
        from agent import PigAgent

        search_tool = MagicMock()
        search_tool.name = "web_search"
        search_tool.execute = lambda x: None

        vol_tool = MagicMock()
        vol_tool.name = "volume_control"

        agent = PigAgent(pg_pool=None, redis=MagicMock(), ctx=None, tools=[], tool_handlers={})

        with patch("tools.create_web_search_tool", return_value=search_tool), \
             patch("tools.volume_tool", vol_tool), \
             patch("tools.search.TavilyProvider", MagicMock()):
            registry = agent._create_default_tools()

        tool = registry.get("web_search")
        assert tool is not None
        assert callable(tool.execute)

    def test_volume_control_has_execute_handler(self):
        from unittest.mock import patch, MagicMock
        from agent import PigAgent

        search_tool = MagicMock()
        search_tool.name = "web_search"

        vol_tool = MagicMock()
        vol_tool.name = "volume_control"
        vol_tool.execute = lambda x: None

        agent = PigAgent(pg_pool=None, redis=MagicMock(), ctx=None, tools=[], tool_handlers={})

        with patch("tools.create_web_search_tool", return_value=search_tool), \
             patch("tools.volume_tool", vol_tool), \
             patch("tools.search.TavilyProvider", MagicMock()):
            registry = agent._create_default_tools()

        tool = registry.get("volume_control")
        assert tool is not None
        assert callable(tool.execute)


class TestBuildRoastBody:
    def test_prompt_only(self):
        from roast.activate import _build_roast_body
        game_mode = MagicMock()
        game_mode.system_prompt_extension = ""
        body = _build_roast_body(game_mode_obj=game_mode, prompt="News text")
        assert "## News Context" in body
        assert "News text" in body
        assert "## Game Mode" not in body

    def test_extension_only(self):
        from roast.activate import _build_roast_body
        game_mode = MagicMock()
        game_mode.system_prompt_extension = "Rules"
        body = _build_roast_body(game_mode_obj=game_mode, prompt="")
        assert "## Game Mode" in body
        assert "Rules" in body
        assert "## News Context" not in body

    def test_both(self):
        from roast.activate import _build_roast_body
        game_mode = MagicMock()
        game_mode.system_prompt_extension = "Rules"
        body = _build_roast_body(game_mode_obj=game_mode, prompt="News")
        assert "## News Context" in body
        assert "## Game Mode" in body

    def test_empty(self):
        from roast.activate import _build_roast_body
        game_mode = MagicMock()
        game_mode.system_prompt_extension = ""
        body = _build_roast_body(game_mode_obj=game_mode, prompt="")
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
        assert agent.model == "qwen-plus-us"


# ── session info ───────────────────────────────────────────────────────────────


class TestBuildSessionInfo:
    def test_has_session_start_tag(self):
        agent, ctx, redis, pg = _make_agent()
        info = agent.build_session_info()
        assert "[Session Start]" in info

    def test_has_current_time(self):
        agent, ctx, redis, pg = _make_agent()
        info = agent.build_session_info()
        assert "Current time:" in info

    def test_includes_timezone(self):
        agent, ctx, redis, pg = _make_agent()
        info = agent.build_session_info()
        # Pacific timezone (PDT or PST)
        assert any(tz in info for tz in ("PDT", "PST", "-07", "-08"))


class TestSeedSessionInfo:
    def test_persists_system_message(self):
        agent, ctx, redis, pg = _make_agent()
        import asyncio
        asyncio.run(agent.seed_session_info("u1"))
        ctx.add_turn.assert_called_once()
        call_kwargs = ctx.add_turn.call_args.kwargs
        assert call_kwargs["user_id"] == "u1"
        assert call_kwargs["role"] == "system"
        assert "[Session Start]" in call_kwargs["content"]

    def test_noop_when_no_ctx(self):
        from agent import PigAgent
        agent = PigAgent(pg_pool=MagicMock(), redis=MagicMock(), ctx=None, tools=[], tool_handlers={})
        import asyncio
        asyncio.run(agent.seed_session_info("u1"))
        # Should not raise

    def test_noop_when_empty_user(self):
        agent, ctx, redis, pg = _make_agent()
        import asyncio
        asyncio.run(agent.seed_session_info(""))
        ctx.add_turn.assert_not_called()


# ── persistence ──────────────────────────────────────────────────────────────


class TestPersistTurns:
    def test_allows_system_messages_through(self):
        agent, ctx, redis, pg = _make_agent()
        from core.llm.types import Message
        msgs = [
            Message.system("session info"),
            Message.user("hello"),
        ]
        import asyncio
        asyncio.run(agent._persist_turns("u1", msgs))
        assert ctx.add_turn.call_count == 2
        roles = [c.kwargs["role"] for c in ctx.add_turn.call_args_list]
        assert roles == ["system", "user"]

    def test_empty_messages_returns_zero(self):
        agent, ctx, redis, pg = _make_agent()
        import asyncio
        result = asyncio.run(agent._persist_turns("u1", []))
        assert result == 0


# ── helpers ─────────────────────────────────────────────────────────────────


async def _run_collect(gen):
    chunks = []
    async for t in gen:
        chunks.append(t)
    return "".join(chunks)
