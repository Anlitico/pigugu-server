"""Web Search Tool  -  creates a Tool from a SearchProvider.

Each provider (Perplexity, Tavily, etc.) implements the SearchProvider ABC.
This module wraps any provider into a Tool that the LLM can call via function calling.

Usage:
    from tools.search import PerplexityProvider
    from tools.web_search import create_web_search_tool

    provider = PerplexityProvider(model="sonar")
    tool = create_web_search_tool(provider)
"""

from __future__ import annotations

from typing import Any

from core.agent.tool import Tool
from tools.search.base import SearchProvider


def create_web_search_tool(provider: SearchProvider) -> Tool:
    """Create a web_search Tool backed by the given SearchProvider.

    The handler delegates to provider.search() and returns only content
    and citations to keep the LLM context clean.
    """

    async def _handler(args: dict) -> dict[str, Any]:
        result = await provider.search(query=args["query"])
        return {
            "content": result.content,
            "citations": result.citations,
        }

    return Tool(
        name="web_search",
        description=(
            "Search the web for information. "
            "Returns synthesized answers with source citations."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query to execute",
                },
            },
            "required": ["query"],
        },
        execute=_handler,
    )
