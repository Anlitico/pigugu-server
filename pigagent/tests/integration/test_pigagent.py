# tests/integration/test_pigagent.py
"""Integration tests for PigAgent — needs DASHSCOPE_US_API_KEY in .env."""

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent.parent.parent / ".env", override=True)


# ── Helpers ────────────────────────────────────────────────────────────────


def _make_test_prompt_store():
    """Build a PromptStore pre-populated from ``prompts/templates/*.j2`` files.

    Reads from the single consolidated template directory. Each test agent
    gets a fresh PromptStore with all 14 prompts preloaded — no PG needed.
    """
    from pathlib import Path
    from prompts import PromptStore

    templates_dir = Path(__file__).parent.parent.parent / "prompts" / "templates"

    store = PromptStore()  # no PG pool
    for name in (
        "global", "trump", "free_chat_marker",
        "roast_together_system", "roast_together_director", "roast_together_ending",
        "debate_bicker_system", "debate_bicker_director",
        "debate_bicker_ending", "debate_bicker_user_won", "debate_bicker_repeat",
    ):
        path = templates_dir / f"{name}.j2"
        if path.is_file():
            store.preload(name, path.read_text(encoding="utf-8"))
    return store


def _make_test_agent():
    """Create a PigAgent with real LLM, mocked storage."""
    from agent import PigAgent
    from system_prompts import PersonaRegistry

    PersonaRegistry.register_defaults()
    prompt_store = _make_test_prompt_store()

    return PigAgent(
        "int-test",
        ctx=None,
        redis=MagicMock(),
        pg_pool=MagicMock(),
        model="qwen3.6-flash",
        prompt_store=prompt_store,
        game_modes={},
        tools=[],
        tool_handlers={},
        temperature=0.6,
        max_tokens=200,
        max_iterations=3,
    )


async def _collect(gen):
    chunks = []
    async for t in gen:
        chunks.append(t)
    return "".join(chunks)


# ── Tests ──────────────────────────────────────────────────────────────────


@pytest.mark.integration
class TestGenerateReply:
    def test_basic_reply(self):
        if not os.getenv("DASHSCOPE_US_API_KEY"):
            pytest.skip("DASHSCOPE_US_API_KEY not set")

        agent = _make_test_agent()

        import asyncio
        result = asyncio.run(_collect(
            agent.generate_reply("Hello! How are you?")
        ))
        assert len(result) > 0, "Empty response from LLM"

    def test_reply_with_persona(self):
        if not os.getenv("DASHSCOPE_US_API_KEY"):
            pytest.skip("DASHSCOPE_US_API_KEY not set")

        agent = _make_test_agent()

        import asyncio
        result = asyncio.run(_collect(
            agent.generate_reply("What is your opinion on technology?",
                                 persona_id=1)
        ))
        assert len(result) > 0


@pytest.mark.integration
class TestStream:
    def test_basic_stream(self):
        if not os.getenv("DASHSCOPE_US_API_KEY"):
            pytest.skip("DASHSCOPE_US_API_KEY not set")

        agent = _make_test_agent()
        from core.llm.types import Message

        import asyncio
        result = asyncio.run(_collect(
            agent.stream([Message.user("Count from 1 to 3.")])
        ))
        assert len(result) > 0


@pytest.mark.integration
class TestStartRoast:
    def test_start_roast_streams_reply(self):
        if not os.getenv("DASHSCOPE_US_API_KEY"):
            pytest.skip("DASHSCOPE_US_API_KEY not set")
        redis_url = os.getenv("REDIS_URL")
        if not redis_url:
            pytest.skip("REDIS_URL not set — roast needs Redis")

        import redis.asyncio as aioredis
        redis_client = aioredis.from_url(redis_url, decode_responses=True)

        from roast import GameModeRegistry
        GameModeRegistry.register_defaults()
        game_modes = GameModeRegistry.build_cache()

        from agent import PigAgent
        from system_prompts import PersonaRegistry
        PersonaRegistry.register_defaults()
        prompt_store = _make_test_prompt_store()

        from context.manager import ContextManager
        ctx = ContextManager("integration-test", redis_client=redis_client, pg_pool=None)

        agent = PigAgent(
            "integration-test",
            ctx=ctx,
            redis=redis_client,
            pg_pool=None,
            model="qwen3.6-flash",
            prompt_store=prompt_store,
            game_modes=game_modes,
            tools=[],
            tool_handlers={},
            temperature=0.6,
            max_tokens=200,
            max_iterations=3,
        )

        import asyncio
        result = asyncio.run(_collect(
            agent.start_roast(
                1, "test-roast-id", "roast_together",
                "Donald Trump tweeted about AI today. Share your thoughts.",
            )
        ))
        assert len(result) > 0, "Empty roast reply"

        # Cleanup
        asyncio.run(agent.close_roast())
