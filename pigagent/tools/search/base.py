"""SearchProvider ABC — standalone search API interface for tool-based web search.

Concrete providers (Perplexity, Tavily, etc.) implement the search() method.
The Tool handler in tools/web_search.py delegates to any SearchProvider instance.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class SearchResult:
    """Normalized search result across all providers."""

    content: str
    citations: list[str]
    provider: str
    model: str
    took_ms: int | None = None


class SearchProvider(ABC):
    """ABC for standalone search APIs (Perplexity, Tavily, etc.).

    Each provider implements search() to execute a query against its backend
    and return a normalized SearchResult.
    """

    @abstractmethod
    async def search(self, query: str) -> SearchResult:
        """Execute a search and return structured results."""
        ...
