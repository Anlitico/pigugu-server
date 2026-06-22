"""PromptStore — per-agent lazy-loading prompt cache backed by PostgreSQL.

Each PigAgent instance owns one PromptStore. On first access to a prompt,
it loads the template text from the `prompt_templates` table and caches it
in memory. Subsequent accesses within the same session are zero-IO.

If PG is unavailable or the prompt is not found, PromptStore falls back to
``prompts/templates/<name>.j2`` files. Only if BOTH sources fail does it
return an empty string.

This means: update a prompt in PG → restart the session (new PigAgent →
new PromptStore) → new prompt takes effect. No redeploy needed.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import asyncpg  # type: ignore[import-untyped]
import jinja2
from loguru import logger

# ── File fallback directory — single source for all .j2 templates ─────────────

_TEMPLATES_DIR = Path(__file__).parent / "templates"

# ── Persona ID → template name mapping ──────────────────────────────────────

_PERSONA_NAME_MAP: dict[int, str] = {
    1: "trump",
}


class PromptStore:
    """Per-agent prompt cache with lazy PG loading.

    Usage::

        store = PromptStore(pg_pool)
        global_prompt = await store.get("global")
        persona_prompt = await store.render("trump", today="2026-06-22")
        full = await store.build_persona_prompt(1, today="2026-06-22")
    """

    def __init__(self, pg_pool: asyncpg.Pool | None = None):
        self._pg_pool = pg_pool
        self._cache: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._env = jinja2.Environment(loader=jinja2.BaseLoader())

    # ── Public API ──────────────────────────────────────────────────────────

    async def get(self, name: str) -> str:
        """Return raw template text for *name*.

        On first access, loads from PG and caches. Returns ``""`` if the
        prompt is not found in the database.
        """
        if name in self._cache:
            return self._cache[name]
        await self._load(name)
        return self._cache.get(name, "")

    async def render(self, name: str, **variables) -> str:
        """Load and render a Jinja2 template by name.

        If *variables* is empty and the template has no Jinja2 syntax,
        this is equivalent to :meth:`get`.
        """
        template = await self.get(name)
        if not template:
            return ""
        if not variables:
            return template
        try:
            return self._env.from_string(template).render(**variables)
        except jinja2.TemplateError as e:
            logger.error(f"[PromptStore] render '{name}' failed: {e}")
            return template  # best-effort: return raw template

    async def build_persona_prompt(
        self, persona_id: int, **extra_vars
    ) -> str:
        """Build the full system prompt for a persona.

        Combines ``global`` + persona-specific template (e.g. ``trump``).
        """
        global_prompt = await self.get("global")
        persona_name = _PERSONA_NAME_MAP.get(persona_id, "")
        persona_prompt = ""
        if persona_name:
            persona_prompt = await self.render(persona_name, **extra_vars)
        else:
            logger.warning(
                f"[PromptStore] Unknown persona_id={persona_id}, "
                f"using global prompt only"
            )
        if global_prompt and persona_prompt:
            return f"{global_prompt}\n\n{persona_prompt}"
        return global_prompt or persona_prompt

    def preload(self, name: str, content: str) -> None:
        """Pre-populate the cache with *content* for *name*.

        Useful in tests and development to seed prompts from files when
        no PG connection is available.
        """
        self._cache[name] = content

    # ── Internal ────────────────────────────────────────────────────────────

    async def _load(self, name: str) -> None:
        """Load a single prompt from PG, with file fallback.

        Priority:
        1. PostgreSQL (authoritative)
        2. ``prompts/templates/<name>.j2`` file (safety net)
        3. Empty string (degraded)
        """
        # Use a lock so concurrent accesses for the same name only hit PG once.
        async with self._lock:
            # Double-check: another task may have loaded it while we waited.
            if name in self._cache:
                return

            # ── 1. Try PostgreSQL ──────────────────────────────────────────
            if self._pg_pool:
                try:
                    async with self._pg_pool.acquire() as conn:
                        row = await conn.fetchrow(
                            "SELECT content FROM prompt_templates "
                            "WHERE name = $1 AND is_active = TRUE",
                            name,
                        )
                        if row:
                            content = row["content"]
                            self._cache[name] = content
                            h = hashlib.md5(content.encode()).hexdigest()[:8]
                            logger.info(
                                f"[PromptStore] Loaded '{name}' from PG "
                                f"({len(content)} chars, md5={h})"
                            )
                            return
                        logger.warning(
                            f"[PromptStore] Prompt '{name}' not found in PG"
                        )
                except Exception as e:
                    logger.error(
                        f"[PromptStore] PG query failed for '{name}': {e}"
                    )
            else:
                logger.warning(
                    f"[PromptStore] No PG pool — skipping PG for '{name}'"
                )

            # ── 2. Fallback: load from .j2 file ────────────────────────────
            try:
                path = _TEMPLATES_DIR / f"{name}.j2"
                if path.is_file():
                    self._cache[name] = path.read_text(encoding="utf-8")
                    logger.warning(
                        f"[PromptStore] Loaded '{name}' from file fallback"
                    )
                    return
            except Exception as e:
                logger.error(
                    f"[PromptStore] File fallback failed for '{name}': {e}"
                )

            # ── 3. Degraded: nothing worked ─────────────────────────────────
            logger.error(
                f"[PromptStore] No source available for '{name}' — "
                f"using empty string"
            )
            self._cache[name] = ""
