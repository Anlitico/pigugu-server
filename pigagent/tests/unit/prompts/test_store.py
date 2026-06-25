"""Unit tests for PromptStore — lazy loading, PG/file fallback, caching."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from prompts import PromptStore


# ── Helpers ────────────────────────────────────────────────────────────────


def _make_pg_pool(fetchrow_result=None, fetchrow_error=None):
    """Build a mock asyncpg pool.

    - *fetchrow_result*: the row dict returned by ``conn.fetchrow()``
    - *fetchrow_error*: an exception to raise from ``conn.fetchrow()``
    """
    conn = AsyncMock()
    if fetchrow_error:
        conn.fetchrow = AsyncMock(side_effect=fetchrow_error)
    else:
        conn.fetchrow = AsyncMock(return_value=fetchrow_result)
    conn.execute = AsyncMock()

    pool = MagicMock()
    pool.acquire = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    return pool


# ── Tests: _load() fallback chain ──────────────────────────────────────────


class TestLoadFromPG:
    def test_pg_success(self):
        """PG returns a row → content is cached."""
        pool = _make_pg_pool(fetchrow_result={"content": "PG content"})
        store = PromptStore(pg_pool=pool)

        result = asyncio.run(store.get("global"))
        assert result == "PG content"

    def test_pg_not_found_falls_back_to_file(self):
        """PG returns no row → load from .j2 file."""
        pool = _make_pg_pool(fetchrow_result=None)  # no row
        store = PromptStore(pg_pool=pool)

        result = asyncio.run(store.get("global"))
        # global.j2 exists in prompts/templates/
        assert "Pigugu" in result
        assert "web_search" in result

    def test_pg_error_falls_back_to_file(self):
        """PG raises → load from .j2 file."""
        pool = _make_pg_pool(fetchrow_error=RuntimeError("PG down"))
        store = PromptStore(pg_pool=pool)

        result = asyncio.run(store.get("global"))
        assert "Pigugu" in result

    def test_no_pg_pool_falls_back_to_file(self):
        """No PG pool at all → load from .j2 file."""
        store = PromptStore()  # pg_pool=None

        result = asyncio.run(store.get("global"))
        assert "Pigugu" in result

    def test_all_sources_fail_returns_empty(self):
        """PG fails + .j2 file doesn't exist → empty string."""
        pool = _make_pg_pool(fetchrow_result=None)
        store = PromptStore(pg_pool=pool)

        result = asyncio.run(store.get("nonexistent_prompt"))
        assert result == ""

    def test_file_not_found_then_empty_cache(self):
        """After all sources fail, subsequent gets return '' from cache (no re-IO)."""
        pool = _make_pg_pool(fetchrow_result=None)
        store = PromptStore(pg_pool=pool)

        # First call triggers load attempt
        r1 = asyncio.run(store.get("nonexistent_prompt"))
        assert r1 == ""

        # Second call hits cache immediately
        r2 = asyncio.run(store.get("nonexistent_prompt"))
        assert r2 == ""

        # PG was queried exactly once (subsequent hits are cache-only)
        conn = pool.acquire.return_value.__aenter__.return_value
        assert conn.fetchrow.call_count == 1


# ── Tests: caching behavior ───────────────────────────────────────────────


class TestCaching:
    def test_cache_hit_avoids_pg(self):
        """Second get() on same name hits cache, no PG query."""
        pool = _make_pg_pool(fetchrow_result={"content": "cached"})
        store = PromptStore(pg_pool=pool)

        r1 = asyncio.run(store.get("global"))
        r2 = asyncio.run(store.get("global"))

        assert r1 == "cached"
        assert r2 == "cached"
        # Only one PG query
        conn = pool.acquire.return_value.__aenter__.return_value
        assert conn.fetchrow.call_count == 1

    def test_preload_then_get_hits_cache(self):
        """preload() seeds cache → get() returns without PG."""
        pool = _make_pg_pool()
        store = PromptStore(pg_pool=pool)
        store.preload("custom", "custom content")

        result = asyncio.run(store.get("custom"))
        assert result == "custom content"

        # PG was never queried
        conn = pool.acquire.return_value.__aenter__.return_value
        conn.fetchrow.assert_not_called()

    def test_different_names_independent_cache(self):
        """Two different prompt names each trigger their own PG query."""
        pool = _make_pg_pool(fetchrow_result={"content": "ok"})
        store = PromptStore(pg_pool=pool)

        asyncio.run(store.get("global"))
        asyncio.run(store.get("trump"))

        conn = pool.acquire.return_value.__aenter__.return_value
        assert conn.fetchrow.call_count == 2


# ── Tests: render() ───────────────────────────────────────────────────────


class TestRender:
    def test_render_no_variables(self):
        """Template with no variables — render() passes through raw text."""
        pool = _make_pg_pool(fetchrow_result={"content": "plain text"})
        store = PromptStore(pg_pool=pool)

        result = asyncio.run(store.render("test"))
        assert result == "plain text"

    def test_render_with_variables(self):
        """Jinja2 variables are rendered correctly."""
        store = PromptStore()
        store.preload("greeting", "Hello {{ user_name }}!")

        result = asyncio.run(store.render("greeting", user_name="World"))
        assert result == "Hello World!"

    def test_render_template_error_returns_raw(self):
        """Jinja2 syntax error → fallback to raw template text."""
        store = PromptStore()
        store.preload("broken", "Hello {{ missing_var")

        result = asyncio.run(store.render("broken"))
        # Should return raw text, not crash
        assert result == "Hello {{ missing_var"

    def test_render_missing_var_uses_empty(self):
        """Undefined Jinja2 variable → rendered as empty string."""
        store = PromptStore()
        store.preload("tmpl", "Value: {{ x }}")

        result = asyncio.run(store.render("tmpl"))
        assert "Value: " in result  # {{ x }} → ""


# ── Tests: build_persona_prompt() ─────────────────────────────────────────


class TestBuildPersonaPrompt:
    def test_combines_global_and_persona(self):
        """build_persona_prompt joins global + persona template."""
        store = PromptStore()
        store.preload("global", "GLOBAL")
        store.preload("trump", "PERSONA {{ today }}")

        result = asyncio.run(
            store.build_persona_prompt(1, today="2026-06-22")
        )
        assert result == "GLOBAL\n\nPERSONA 2026-06-22"

    def test_unknown_persona_id_uses_global_only(self):
        """Persona ID not in map → only global prompt returned."""
        store = PromptStore()
        store.preload("global", "GLOBAL")

        result = asyncio.run(store.build_persona_prompt(999))
        assert result == "GLOBAL"

    def test_persona_without_global(self):
        """No global prompt → only persona prompt returned."""
        store = PromptStore()
        store.preload("global", "")
        store.preload("trump", "PERSONA")

        result = asyncio.run(store.build_persona_prompt(1))
        assert result == "PERSONA"

    def test_filename_equals_pg_name(self):
        """Template name (without .j2) matches PG prompt_templates.name."""
        # This test verifies the naming convention: prompt name "global"
        # maps to file "prompts/templates/global.j2" and PG row name="global".
        from pathlib import Path
        import os

        templates_dir = Path(__file__).parent.parent.parent.parent / "prompts" / "templates"
        for j2_file in templates_dir.glob("*.j2"):
            name = j2_file.stem  # filename without .j2 extension
            # The name should be a valid prompt identifier (no path separators, etc.)
            assert "/" not in name
            assert "\\" not in name
            assert name  # non-empty
            # Verify we can load it through PromptStore file fallback
            store = PromptStore()
            content = asyncio.run(store.get(name))
            assert content, f"Prompt '{name}' loaded empty from file fallback"


# ── Tests: concurrent access ──────────────────────────────────────────────


class TestConcurrency:
    def test_concurrent_same_name_single_pg_query(self):
        """Multiple concurrent get() for the same name → only one PG query."""
        pool = _make_pg_pool(fetchrow_result={"content": "shared"})
        store = PromptStore(pg_pool=pool)

        async def _concurrent():
            tasks = [store.get("global") for _ in range(5)]
            results = await asyncio.gather(*tasks)
            return results

        results = asyncio.run(_concurrent())
        assert all(r == "shared" for r in results)

        # Only one PG query despite 5 concurrent gets
        conn = pool.acquire.return_value.__aenter__.return_value
        assert conn.fetchrow.call_count == 1
