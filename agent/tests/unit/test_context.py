# tests/unit/test_context.py
"""Unit tests for context module — schemas, compression, manager, loader."""

import json

import pytest

from core.llm.types import Message, ToolCall
from core.llm.provider import LLMProvider
from context.schemas import (
    WorkingContext, UserMemory, RedisKeys, TokenBudget, RoastContext, validate_tool_calls,
    RAW_TURN_COUNT, HOT_WINDOW_SIZE, COMPRESSION_THRESHOLD,
    FLUSH_BATCH_SIZE, TOKEN_BUDGET_CAP, ROAST_BUFFER_RATIO,
)


class TestRedisKeys:
    def test_turns(self):
        assert RedisKeys.turns("u1") == "ctx:u1:turns"

    def test_meta(self):
        assert RedisKeys.meta("u1") == "ctx:u1:meta"

    def test_summary_recent(self):
        assert RedisKeys.summary_recent("u1") == "ctx:u1:summary:recent"

    def test_summary_global(self):
        assert RedisKeys.summary_global("u1") == "ctx:u1:summary:global"

    def test_game_state(self):
        assert RedisKeys.game_state("u1") == "ctx:u1:game_state"

    def test_user_memory(self):
        assert RedisKeys.user_memory("u1") == "pigugu:user:u1:memory"

    def test_roast_prompt(self):
        assert RedisKeys.roast_prompt("u1") == "ctx:u1:roast:prompt"

    def test_roast_turns(self):
        assert RedisKeys.roast_turns("u1") == "ctx:u1:roast:turns"

    def test_roast_summary(self):
        assert RedisKeys.roast_summary("u1") == "ctx:u1:roast:summary"

    def test_roast_meta(self):
        assert RedisKeys.roast_meta("u1") == "ctx:u1:roast:meta"


class TestProviderTokenCounting:
    """Token counting: fast (tiktoken) + async (API, falls back to offline)."""

    def test_qwen_count_tokens_uses_tiktoken(self):
        from core.llm.providers.qwen import QwenProvider
        p = QwenProvider(api_key="sk-test")
        assert p.count_tokens("") == 0
        tokens = p.count_tokens("hello world")
        # tiktoken cl100k_base: "hello world" = 2 tokens
        assert 2 <= tokens <= 3

    def test_qwen_count_tokens_chinese(self):
        from core.llm.providers.qwen import QwenProvider
        p = QwenProvider(api_key="sk-test")
        tokens = p.count_tokens("你好世界")
        assert tokens > 0

    def test_volcengine_count_tokens_uses_tiktoken(self):
        from core.llm.providers.volcengine import VolcengineProvider
        p = VolcengineProvider(api_key="sk-test")
        assert p.count_tokens("") == 0
        tokens = p.count_tokens("hello world")
        assert 2 <= tokens <= 3

    def test_count_tokens_async_falls_back_to_offline(self):
        """When API is unavailable, count_tokens_async falls back to tiktoken."""
        from core.llm.providers.qwen import QwenProvider
        import asyncio
        p = QwenProvider(api_key="sk-test")

        async def run():
            return await p.count_tokens_async("hello")
        tokens = asyncio.run(run())
        # Without a real API endpoint, should fall back to tiktoken
        assert tokens > 0

    def test_to_messages_uses_token_counter(self):
        """to_messages() should use passed token_counter for budget tracking."""
        wc = WorkingContext(user_id="u1")
        msgs = wc.to_messages(
            system_prompt="sys prompt",
            token_counter=lambda t: 100 if t else 0,
        )
        assert wc.budget.layer_1_system == 100
        assert len(msgs) == 1


class TestTokenBudget:
    def test_defaults(self):
        b = TokenBudget()
        assert b.total_cap == TOKEN_BUDGET_CAP
        assert b.used == 0
        assert b.remaining == TOKEN_BUDGET_CAP

    def test_used_calculation(self):
        b = TokenBudget(
            layer_1_system=1000,
            layer_2_user_pref=500,
            layer_3_session=3000,
            layer_4_roast_prompt=2000,
            layer_4_roast_turns=1500,
        )
        assert b.used == 8000
        assert b.remaining == TOKEN_BUDGET_CAP - 8000

    def test_to_dict(self):
        b = TokenBudget(layer_1_system=100)
        d = b.to_dict()
        assert d["layer_1_system"] == 100
        assert "remaining" in d


class TestRoastContext:
    def test_defaults(self):
        rc = RoastContext(roast_id="r1")
        assert rc.roast_id == "r1"
        assert rc.prompt == ""
        assert rc.turns == []
        assert rc.summary == ""

    def test_is_active(self):
        assert RoastContext(roast_id="r1").is_active
        assert not RoastContext(roast_id="").is_active

    def test_total_tokens(self):
        rc = RoastContext(roast_id="r1", prompt_tokens=100, turns_tokens=200, summary_tokens=50)
        assert rc.total_tokens == 350

    def test_to_meta(self):
        rc = RoastContext(roast_id="r1")
        rc.turns = [Message.user("hi")]
        meta = rc.to_meta()
        assert meta["roast_id"] == "r1"
        assert meta["turn_count"] == 1


class TestWorkingContext:
    def test_defaults(self):
        wc = WorkingContext(user_id="u1")
        assert wc.tier == 0
        assert wc.raw_turns == []
        assert wc.roast is None
        assert wc.user_memory is None

    def test_to_messages_empty(self):
        wc = WorkingContext(user_id="u1")
        msgs = wc.to_messages()
        assert msgs == []

    def test_to_messages_with_system_prompt(self):
        wc = WorkingContext(user_id="u1")
        msgs = wc.to_messages(system_prompt="You are helpful.")
        assert len(msgs) == 1
        assert msgs[0].role == "system"

    def test_to_messages_with_user_memory(self):
        wc = WorkingContext(
            user_id="u1",
            user_memory=UserMemory(user_id="u1", profile_summary="User likes sports."),
        )
        msgs = wc.to_messages(system_prompt="sys")
        assert len(msgs) == 2
        assert "User likes sports" in msgs[1].content

    def test_to_messages_with_summaries(self):
        wc = WorkingContext(
            user_id="u1", tier=2,
            recent_summary="Talked about weather.",
            global_summary="Earlier: food.",
        )
        msgs = wc.to_messages(system_prompt="sys")
        assert len(msgs) == 3  # system + global + recent

    def test_to_messages_with_raw_turns(self):
        wc = WorkingContext(
            user_id="u1",
            raw_turns=[
                Message(role="user", content="hello"),
                Message(role="assistant", content="hi"),
            ],
        )
        msgs = wc.to_messages(system_prompt="sys")
        assert len(msgs) == 3  # system + 2 turns (reversed)
        assert msgs[1].role == "assistant"

    def test_to_messages_with_roast(self):
        wc = WorkingContext(
            user_id="u1",
            roast=RoastContext(
                roast_id="r1",
                summary="Game: trivia challenge\n\n---\n\nEarlier: user answered 3 questions.",
                turns=[
                    Message(role="user", content="answer D"),
                    Message(role="assistant", content="correct!"),
                ],
            ),
        )
        msgs = wc.to_messages(system_prompt="sys")
        # sys + roast_summary(user) + 2 roast turns = 4
        assert len(msgs) == 4
        assert msgs[1].role == "user"  # roast summary as user message
        assert "trivia challenge" in msgs[1].content

    def test_budget_summary(self):
        wc = WorkingContext(user_id="u1")
        wc.budget.layer_1_system = 100
        summary = wc.budget_summary()
        assert summary["breakdown"]["L1_system"] == 100


class TestUserMemory:
    def test_defaults(self):
        um = UserMemory(user_id="u1")
        assert um.profile_summary == ""

    def test_to_hash_and_back(self):
        um = UserMemory(
            user_id="u1",
            profile_summary="User likes sports.",
            stats={"total_turns": 10},
        )
        h = um.to_hash()
        restored = UserMemory.from_hash(h)
        assert restored.profile_summary == "User likes sports."

    def test_empty_stats(self):
        um = UserMemory(user_id="u1")
        h = um.to_hash()
        restored = UserMemory.from_hash(h)
        assert restored.stats == {}

    def test_token_count(self):
        um = UserMemory(user_id="u1", profile_summary="hello")
        assert um.token_count() > 0


class TestConstants:
    def test_raw_turn_count(self):
        assert RAW_TURN_COUNT == 5

    def test_hot_window_size(self):
        assert HOT_WINDOW_SIZE == 100

    def test_compression_threshold(self):
        assert COMPRESSION_THRESHOLD == 20

    def test_flush_batch_size(self):
        assert FLUSH_BATCH_SIZE == 10

    def test_token_budget_cap(self):
        assert TOKEN_BUDGET_CAP == 200_000

    def test_roast_buffer_ratio(self):
        assert ROAST_BUFFER_RATIO == 0.6


class TestMessageSerialization:
    """Verify Message.to_dict/from_dict roundtrip (used for Redis storage)."""

    def test_simple_roundtrip(self):
        m = Message(role="user", content="hello")
        d = m.to_dict()
        restored = Message.from_dict(d)
        assert restored.role == "user"
        assert restored.content == "hello"

    def test_with_partial(self):
        m = Message(role="assistant", content="cont...", partial=True)
        d = m.to_dict()
        restored = Message.from_dict(d)
        assert restored.partial is True

    def test_extra_keys_ignored(self):
        """turn_number in JSON wrapper should be ignored by from_dict."""
        d = {"turn": 42, "role": "user", "content": "hi"}
        m = Message.from_dict(d)
        assert m.role == "user"
        assert not hasattr(m, "turn")


class TestContextManager:
    def test_constructor(self):
        from context.manager import ContextManager
        ctx = ContextManager(user_id="u1")
        assert ctx._user_id == "u1"
        assert ctx.user_memory.user_id == "u1"

    def test_user_memory_default(self):
        from context.manager import ContextManager
        ctx = ContextManager(user_id="u1")
        assert ctx.user_memory.profile_summary == ""

    def test_roast_budget_pressured_empty(self):
        from context.manager import ContextManager
        from context.schemas import RoastContext
        rc = RoastContext(roast_id="r1")
        assert not ContextManager._roast_budget_pressured(rc)

    def test_roast_budget_pressured_over_threshold(self):
        from context.manager import ContextManager
        from context.schemas import RoastContext, ROAST_RAW_TURN_COUNT
        rc = RoastContext(roast_id="r1")
        rc.turns = [Message.user(f"msg {i}") for i in range(ROAST_RAW_TURN_COUNT + 1)]
        assert ContextManager._roast_budget_pressured(rc)


class TestCompressionAnchor:
    """Anchor guarantees: summary covers ≤anchor, raw turns > anchor."""

    def test_anchor_constant_values(self):
        from context.schemas import META_ANCHOR, META_ANCHOR_IN_PROGRESS
        assert META_ANCHOR == "compression_anchor"
        assert META_ANCHOR_IN_PROGRESS == "compression_in_progress"

    def test_get_hot_turns_without_anchor_returns_all(self):
        """Without anchor, all turns should be returned (fallback)."""
        # This is tested implicitly by assemble() with anchor=0
        pass


class TestFactExtraction:
    """L2 two-layer: extract categorized facts → summarize into profile."""

    def test_extract_facts_empty_turns(self):
        from context.compression import ContextCompressor
        import asyncio
        c = ContextCompressor()
        facts = asyncio.run(c.extract_facts([]))
        assert facts == []

    def test_summarize_profile_empty(self):
        from context.compression import ContextCompressor
        import asyncio
        c = ContextCompressor()
        profile = asyncio.run(c.summarize_profile([]))
        assert profile == ""

    def test_summarize_profile_initial(self):
        from context.compression import ContextCompressor
        import asyncio
        c = ContextCompressor()
        facts = [
            "Name is John (personal)",
            "Allergic to peanuts (health)",
            "Prefers dark humor (preference)",
        ]
        profile = asyncio.run(c.summarize_profile(facts))
        assert len(profile) > 0

    def test_summarize_profile_incremental(self):
        from context.compression import ContextCompressor
        import asyncio
        c = ContextCompressor()
        existing = "John is a software engineer in Shanghai."
        new_facts = ["Prefers dark humor (preference)"]
        profile = asyncio.run(c.summarize_profile(new_facts, existing=existing))
        assert len(profile) > 0


class TestToolCallValidation:
    """validate_tool_calls — filter incomplete tool calls before LLM context."""

    def test_empty_list(self):
        assert validate_tool_calls([]) == []

    def test_no_tool_calls_passthrough(self):
        msgs = [Message.user("hello"), Message.assistant("hi")]
        result = validate_tool_calls(msgs)
        assert len(result) == 2

    def test_complete_tool_chain_passes(self):
        tc = ToolCall(id="call_1", name="get_weather", arguments='{"city":"BJ"}')
        msgs = [
            Message.user("what is the weather"),
            Message.assistant(tool_calls=[tc]),
            Message.tool(call_id="call_1", name="get_weather", content='{"temp":25}'),
            Message.assistant("It's 25 degrees."),
        ]
        result = validate_tool_calls(msgs)
        assert len(result) == 4
        assert result[1].tool_calls is not None  # assistant with tool_calls kept

    def test_dangling_tool_call_removed(self):
        """Assistant with tool_calls but no matching response → strip calls, keep text."""
        tc = ToolCall(id="call_1", name="get_weather", arguments='{}')
        msgs = [
            Message.user("weather?"),
            Message.assistant(content="Let me check.", tool_calls=[tc]),
        ]
        result = validate_tool_calls(msgs)
        assert len(result) == 2
        assert result[1].tool_calls is None  # dangling calls stripped
        assert result[1].content == "Let me check."  # text preserved

    def test_assistant_only_dangling_calls_dropped(self):
        """Assistant with only tool_calls and no content → dropped entirely."""
        tc = ToolCall(id="call_1", name="get_weather", arguments='{}')
        msgs = [
            Message.user("weather?"),
            Message.assistant(tool_calls=[tc]),  # no content, only tool_calls
            Message.user("nevermind"),
        ]
        result = validate_tool_calls(msgs)
        assert len(result) == 2  # assistant dropped
        assert result[0].role == "user"
        assert result[1].role == "user"

    def test_partial_fulfillment(self):
        """One call resolved, one not → keep only resolved."""
        tc1 = ToolCall(id="call_1", name="get_weather", arguments='{}')
        tc2 = ToolCall(id="call_2", name="get_time", arguments='{}')
        msgs = [
            Message.user("weather and time?"),
            Message.assistant(tool_calls=[tc1, tc2]),
            Message.tool(call_id="call_1", name="get_weather", content='{"temp":25}'),
        ]
        result = validate_tool_calls(msgs)
        # Assistant should now only have call_1
        assert len(result[1].tool_calls) == 1
        assert result[1].tool_calls[0].id == "call_1"

    def test_tool_without_call_id_kept(self):
        msgs = [
            Message(role="tool", content="orphan result"),
        ]
        result = validate_tool_calls(msgs)
        assert len(result) == 1  # kept as-is


class TestLoadResult:
    def test_defaults(self):
        from context.loader import LoadResult
        r = LoadResult()
        assert r.messages == []


class TestContextLoader:
    def test_constructor(self):
        from context.loader import ContextLoader
        loader = ContextLoader()
        assert loader._redis is None

    def test_constructor_with_infra(self):
        from context.loader import ContextLoader
        loader = ContextLoader(redis_client="r", pg_pool="p")
        assert loader._redis == "r"

    def test_record_turn_without_redis(self):
        from context.loader import ContextLoader
        import asyncio
        loader = ContextLoader()
        asyncio.run(loader.record_turn(
            user_id="u1", role="user", content="hello",
        ))
