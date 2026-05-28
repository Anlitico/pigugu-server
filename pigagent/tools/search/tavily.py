"""Tavily Search Provider  -  Tavily Search API integration.

Uses the Tavily Python SDK for web search with AI-generated answers.
Supports configurable search depth and max results.
"""

from __future__ import annotations

import os
import time
from typing import Optional

from loguru import logger

from tools.search.base import SearchProvider, SearchResult

try:
    from tavily import TavilyClient as _TavilyClient  # type: ignore[assignment]
    _TAVILY_AVAILABLE = True
except ImportError:
    _TavilyClient = None  # type: ignore[assignment]
    _TAVILY_AVAILABLE = False


class TavilyProvider(SearchProvider):
    """Search provider backed by Tavily Search API."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        search_depth: str = "basic",
        max_results: int = 5,
    ):
        if not _TAVILY_AVAILABLE:
            raise RuntimeError(
                "Tavily SDK not installed. Run: pip install tavily-python"
            )

        self._api_key = api_key or os.getenv("TAVILY_API_KEY")
        if not self._api_key:
            raise RuntimeError(
                "No Tavily API key found. Set TAVILY_API_KEY."
            )
        self._search_depth: str = search_depth
        self._max_results = max_results
        self._client = _TavilyClient(api_key=self._api_key)  # type: ignore[misc]

    async def search(self, query: str) -> SearchResult:
        start = time.monotonic()

        logger.info(f"[Tavily] Searching: {query[:80]}...")
        logger.debug(
            f"[Tavily] depth={self._search_depth}, max_results={self._max_results}"
        )

        response = self._client.search(
            query=query,
            search_depth=self._search_depth,  # type: ignore[reportArgumentType]
            max_results=self._max_results,
            include_answer=True,
        )

        took_ms = int((time.monotonic() - start) * 1000)

        content = response.get("answer", "")
        results = response.get("results", [])
        citations = [r.get("url", "") for r in results]

        logger.info(
            f"[Tavily] Done: {took_ms}ms, {len(citations)} citations, "
            f"{len(content)} chars"
        )

        return SearchResult(
            content=content,
            citations=citations,
            provider="tavily",
            model=f"tavily-{self._search_depth}",
            took_ms=took_ms,
        )
