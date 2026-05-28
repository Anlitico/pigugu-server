"""tools.search  -  web search providers for agent tool use.

Exposes:
    SearchProvider      - ABC for standalone search APIs
    SearchResult        - normalized result dataclass
    PerplexityProvider  - Perplexity Sonar / OpenRouter
    TavilyProvider      - Tavily Search API

Usage:
    from tools.search import TavilyProvider

    provider = TavilyProvider()
    result = await provider.search("What's the latest AI news?")
    # result.content, result.citations, result.took_ms, ...
"""

from tools.search.base import SearchProvider, SearchResult
from tools.search.perplexity import PerplexityProvider
from tools.search.tavily import TavilyProvider

__all__ = [
    "SearchProvider",
    "SearchResult",
    "PerplexityProvider",
    "TavilyProvider",
]
