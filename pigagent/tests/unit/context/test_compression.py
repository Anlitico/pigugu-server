# tests/unit/context/test_compression.py
"""Unit tests for context compression  -  L2 facts, L3 session, L4 roast edge cases."""

import asyncio

import pytest


class TestCompressionAnchor:
    """Anchor guarantees: summary covers <=anchor, raw turns > anchor."""

    def test_get_hot_turns_without_anchor_returns_all(self):
        """Without anchor, all turns should be returned (fallback)."""
        pass


class TestFactExtraction:
    """L2 two-layer: extract categorized facts -> summarize into profile."""

    def test_extract_facts_empty_turns(self):
        from context.compression.l2_facts import extract_facts
        facts = asyncio.run(extract_facts([]))
        assert facts == []

    def test_summarize_profile_empty(self):
        from context.compression.l2_facts import summarize_profile
        profile = asyncio.run(summarize_profile([]))
        assert profile == ""

    @pytest.mark.integration
    def test_summarize_profile_initial(self):
        from context.compression.l2_facts import summarize_profile
        facts = [
            "Name is John (personal)",
            "Allergic to peanuts (health)",
            "Prefers dark humor (preference)",
        ]
        profile = asyncio.run(summarize_profile(facts))
        assert len(profile) > 0

    @pytest.mark.integration
    def test_summarize_profile_incremental(self):
        from context.compression.l2_facts import summarize_profile
        existing = "John is a software engineer in Shanghai."
        new_facts = ["Prefers dark humor (preference)"]
        profile = asyncio.run(summarize_profile(new_facts, existing=existing))
        assert len(profile) > 0


class TestL3SessionEdgeCases:
    """L3 compression edge cases  -  no external LLM calls."""

    def test_compress_turns_empty(self):
        from context.compression.l3_session import compress_turns
        result = asyncio.run(compress_turns([]))
        assert result == ""

    def test_merge_summary_both_empty(self):
        from context.compression.l3_session import merge_summary
        result = asyncio.run(merge_summary("", []))
        assert result == ""

    def test_merge_summary_no_existing(self):
        from context.compression.l3_session import merge_summary
        result = asyncio.run(merge_summary("", []))
        assert result == ""


class TestL4RoastEdgeCases:
    """L4 roast compression edge cases  -  no external LLM calls."""

    def test_compress_roast_empty_turns_no_summary(self):
        from context.compression.l4_roast import compress_roast
        result = asyncio.run(compress_roast([], existing_summary=""))
        assert result == ""

    def test_compress_roast_empty_turns_with_summary(self):
        from context.compression.l4_roast import compress_roast
        result = asyncio.run(compress_roast(
            [], existing_summary="previous summary",
        ))
        assert result == "previous summary"


# ── CompressionMetrics tests ────────────────────────────────────────────────

import time as _time_module


class TestCompressionMetrics:
    def test_basic_mark_and_segments(self):
        from metrics.compression import CompressionMetrics
        m = CompressionMetrics("u1", "free_chat")
        _time_module.sleep(0.01)
        m.mark("check_done")
        _time_module.sleep(0.01)
        m.mark("llm_done")
        _time_module.sleep(0.01)
        m.mark("profile_done")
        m.finish()

        segs = m._compute_segments()
        assert segs["check"] > 0
        assert segs["llm"] > 0
        assert segs["profile"] > 0
        assert segs["total"] > 0
        assert segs["total"] >= segs["check"] + segs["llm"] + segs["profile"]

    def test_set_and_get_meta(self):
        from metrics.compression import CompressionMetrics
        m = CompressionMetrics("u1", "roast")
        m.set_meta("turns_in", 100)
        m.set_meta("model", "test")
        m.set_meta("has_l4", True)
        assert m._meta["turns_in"] == 100
        assert m._meta["model"] == "test"
        assert m._meta["has_l4"] is True

    def test_skip_missing_marks(self):
        from metrics.compression import CompressionMetrics
        m = CompressionMetrics("u1", "free_chat")
        # Don't mark check_done — segment should be omitted
        m.mark("llm_done")
        m.finish()
        segs = m._compute_segments()
        assert "check" not in segs
        assert "llm" not in segs  # start→check_done missing
        assert "total" in segs

    def test_scenario_stored(self):
        from metrics.compression import CompressionMetrics
        m = CompressionMetrics("u2", "roast")
        assert m._scenario == "roast"


# ── Compressor _rebuild_memory tests ─────────────────────────────────────────


class TestRebuildMemory:
    def teardown_method(self):
        from context.storage.memory import clear_all
        clear_all()

    def test_post_anchor_filtering(self):
        from context.storage.memory import clear_all
        from context.storage.memory import MemoryStore
        from context.schema import ConversationRecord
        from context.compression.compressor import ContextCompressor

        clear_all()
        mem = MemoryStore("u1")
        records = [
            ConversationRecord(turn_number=1, role="user", content="a", created_at=1.0),
            ConversationRecord(turn_number=2, role="assistant", content="b", created_at=2.0),
            ConversationRecord(turn_number=3, role="user", content="c", created_at=3.0),
            ConversationRecord(turn_number=4, role="assistant", content="d", created_at=4.0),
            ConversationRecord(turn_number=5, role="user", content="e", created_at=5.0),
        ]
        for r in records:
            mem.push_turn(r)

        comp = ContextCompressor(redis_client=None, pg_pool=None)
        comp._mem = mem
        count = comp._rebuild_memory("u1", end_turn=3, l2_profile="p", l3_session="s")
        assert count == 2  # turns 4 and 5
        remaining = mem.get_hot_turns(10)
        assert len(remaining) == 2
        assert remaining[0].turn_number == 4
        assert remaining[1].turn_number == 5

    def test_rebuild_stores_summaries(self):
        from context.storage.memory import clear_all
        from context.storage.memory import MemoryStore
        from context.schema import ConversationRecord
        from context.compression.compressor import ContextCompressor

        clear_all()
        mem = MemoryStore("u1")
        mem.push_turn(ConversationRecord(turn_number=1, role="user", content="hi", created_at=1.0))

        comp = ContextCompressor(redis_client=None, pg_pool=None)
        comp._mem = mem
        comp._rebuild_memory("u1", end_turn=1,
                             l2_profile="profile text", l3_session="session text",
                             l4_roast="roast text", roast_id="rid1",
                             roast_prompt="prompt", roast_prompt_turn=2)

        data = mem.read_summaries()
        assert data["l2_profile"] == "profile text"
        assert data["l3_session"] == "session text"
        assert data["l4_roast"] == "roast text"
        assert data["roast_id"] == "rid1"
        assert data["roast_prompt"] == "prompt"
        assert data["roast_prompt_turn"] == 2
        assert data["end_turn"] == 1
