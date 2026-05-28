"""Perplexity Search Provider  -  Sonar API integration.

Supports both Perplexity native (pplx- key) and OpenRouter (sk-or- key).
Key prefix determines the base URL automatically.
"""

from __future__ import annotations

import os
import time
from typing import Optional

from loguru import logger
from openai import AsyncOpenAI

from tools.search.base import SearchProvider, SearchResult


def _get_api_key() -> Optional[str]:
    return os.getenv("PERPLEXITY_API_KEY") or os.getenv("OPENROUTER_API_KEY")


def _resolve_base_url(api_key: str) -> str:
    if api_key.startswith("pplx-"):
        return "https://api.perplexity.ai"
    if api_key.startswith("sk-or-"):
        return "https://openrouter.ai/api/v1"
    return "https://api.perplexity.ai"


class PerplexityProvider(SearchProvider):
    """Search provider backed by Perplexity Sonar API (or OpenRouter)."""

    def __init__(
        self,
        model: str = "sonar",
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        search_context_size: str = "low",
    ):
        self._model = model
        self._api_key = api_key or _get_api_key()
        if not self._api_key:
            raise RuntimeError(
                "No Perplexity API key found. Set PERPLEXITY_API_KEY or OPENROUTER_API_KEY."
            )
        self._base_url = base_url or _resolve_base_url(self._api_key)
        self._search_context_size = search_context_size

    async def search(self, query: str) -> SearchResult:
        start = time.monotonic()

        client = AsyncOpenAI(api_key=self._api_key, base_url=self._base_url)

        logger.info(f"[Perplexity] Searching: {query[:80]}...")
        logger.debug(f"[Perplexity] model={self._model}, base_url={self._base_url}")

        response = await client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": query}],
            stream=False,
            extra_body={"search_context_size": self._search_context_size},
        )

        took_ms = int((time.monotonic() - start) * 1000)

        content = ""
        if response.choices:
            content = response.choices[0].message.content or ""

        citations: list[str] = []
        if getattr(response, "citations", None):
            citations = response.citations  # type: ignore[union-attr]
        elif (extra := getattr(response, "model_extra", None)):
            citations = extra.get("citations", [])

        logger.info(
            f"[Perplexity] Done: {took_ms}ms, {len(citations)} citations, "
            f"{len(content)} chars"
        )

        return SearchResult(
            content=content,
            citations=citations,
            provider="perplexity",
            model=self._model,
            took_ms=took_ms,
        )
