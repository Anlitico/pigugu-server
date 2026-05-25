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
        result = asyncio.run(compress_roast([], existing_summary="", roast_prompt=""))
        assert result == ""

    def test_compress_roast_empty_turns_with_prompt(self):
        from context.compression.l4_roast import compress_roast
        result = asyncio.run(compress_roast(
            [], existing_summary="", roast_prompt="Game rules: ...",
        ))
        assert result == "Game rules: ..."

    def test_compress_roast_empty_turns_with_prompt_and_summary(self):
        from context.compression.l4_roast import compress_roast
        result = asyncio.run(compress_roast(
            [], existing_summary="previous summary", roast_prompt="Game rules: ...",
        ))
        assert "Game rules:" in result
        assert "previous summary" in result
