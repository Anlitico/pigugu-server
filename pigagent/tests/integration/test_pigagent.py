# tests/integration/test_pigagent.py
"""Integration tests for PigAgent — needs DASHSCOPE_US_API_KEY in .env."""

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent.parent.parent / ".env", override=True)


# ── Helpers ────────────────────────────────────────────────────────────────


def _make_test_agent():
    """Create a PigAgent with real LLM, mocked storage."""
    from agent import PigAgent
    from system_prompts import PersonaRegistry

    PersonaRegistry.register_defaults()
    prompts = PersonaRegistry.build_prompt_cache()

    return PigAgent(
        ctx=None,
        redis=MagicMock(),
        pg_pool=MagicMock(),
        model="qwen3.6-flash",
        prompts=prompts,
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
            agent.generate_reply("integration-test-user", "Hello! How are you?")
        ))
        assert len(result) > 0, "Empty response from LLM"

    def test_reply_with_persona(self):
        if not os.getenv("DASHSCOPE_US_API_KEY"):
            pytest.skip("DASHSCOPE_US_API_KEY not set")

        agent = _make_test_agent()

        import asyncio
        result = asyncio.run(_collect(
            agent.generate_reply("u1", "What is your opinion on technology?",
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
        prompts = PersonaRegistry.build_prompt_cache()

        from context.manager import ContextManager
        ctx = ContextManager(redis_client=redis_client, pg_pool=None)

        agent = PigAgent(
            ctx=ctx,
            redis=redis_client,
            pg_pool=None,
            model="qwen3.6-flash",
            prompts=prompts,
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
                "integration-test", 1, "test-roast-id", "roast_together",
                "Donald Trump tweeted about AI today. Share your thoughts.",
            )
        ))
        assert len(result) > 0, "Empty roast reply"

        # Cleanup
        asyncio.run(agent.close_roast("integration-test"))
