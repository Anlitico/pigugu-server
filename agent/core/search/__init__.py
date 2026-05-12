# agent/core/search/__init__.py
"""LLM 驱动的搜索能力"""

from .adapter import SearchAdapter, build_search_messages, create_search_adapter
from .perplexity import web_search as perplexity_web_search, search_perplexity

__all__ = [
    "SearchAdapter",
    "build_search_messages",
    "create_search_adapter",
    "perplexity_web_search",
    "search_perplexity",
]
