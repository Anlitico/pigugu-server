"""
Perplexity Search Tool

Provides a web_search tool implementation using Perplexity Sonar API.
Returns synthesized answers with citations rather than raw search results.

Reference: perplexity-search.md
"""

import os
from typing import Optional, Any
from dataclasses import dataclass

from openai import AsyncOpenAI
from loguru import logger


@dataclass
class PerplexitySearchResult:
    """Normalized Perplexity search result."""
    query: str
    content: str
    citations: list[str]
    model: str
    provider: str = "perplexity"
    took_ms: Optional[int] = None


def get_perplexity_api_key() -> Optional[str]:
    """
    Resolve Perplexity API key following the priority order:
    1. PERPLEXITY_API_KEY env var
    2. OPENROUTER_API_KEY env var
    
    Returns:
        API key string or None if not found
    """
    return os.getenv("PERPLEXITY_API_KEY") or os.getenv("OPENROUTER_API_KEY")


def infer_base_url_from_key(api_key: str) -> str:
    """
    Infer base URL from API key prefix if possible.
    
    - pplx- -> https://api.perplexity.ai
    - sk-or- -> https://openrouter.ai/api/v1
    
    Returns:
        Inferred base URL or empty string if can't infer
    """
    if api_key.startswith("pplx-"):
        return "https://api.perplexity.ai"
    if api_key.startswith("sk-or-"):
        return "https://openrouter.ai/api/v1"
    return ""


async def search_perplexity(
    query: str,
    model: str = "sonar-pro",
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> PerplexitySearchResult:
    """
    Execute a search query using Perplexity API.
    
    Args:
        query: The search query text
        model: Perplexity model to use (default: sonar-pro)
        base_url: API base URL (defaults to https://api.perplexity.ai or inferred from key)
        api_key: API key (defaults to env var PERPLEXITY_API_KEY or OPENROUTER_API_KEY)
    
    Returns:
        PerplexitySearchResult with synthesized content and citations
    
    Raises:
        RuntimeError: If no API key is available or API call fails
    """
    import time
    
    start_time = time.time()
    
    # Resolve API key
    resolved_key = api_key or get_perplexity_api_key()
    if not resolved_key:
        raise RuntimeError(
            "No Perplexity API key found. Set PERPLEXITY_API_KEY or OPENROUTER_API_KEY environment variable."
        )
    
    # Resolve base URL
    resolved_base_url = base_url
    if not resolved_base_url:
        inferred = infer_base_url_from_key(resolved_key)
        resolved_base_url = inferred or "https://api.perplexity.ai"
    
    client = AsyncOpenAI(api_key=resolved_key, base_url=resolved_base_url)
    
    logger.info(f"🔍 [PERPLEXITY] Searching: {query[:80]}...")
    logger.debug(f"🔍 [PERPLEXITY] Model: {model}, Base URL: {resolved_base_url}")
    
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "user", "content": query}
            ],
            stream=False,
        )
        
        took_ms = int((time.time() - start_time) * 1000)
        
        # Extract content
        content = (response.choices[0].message.content or "") if response.choices else ""
        
        # Extract citations from response metadata if available
        citations: list[str] = []
        if hasattr(response, "citations") and response.citations:
            citations = response.citations
        elif hasattr(response, "model_extra") and response.model_extra:
            # Some providers put citations in model_extra
            citations = response.model_extra.get("citations", [])
        
        result = PerplexitySearchResult(
            query=query,
            content=content,
            citations=citations,
            model=model,
            took_ms=took_ms,
        )
        
        logger.info(
            f"🔍 [PERPLEXITY] Search complete: {took_ms}ms, "
            f"{len(citations)} citations, {len(content)} chars content"
        )
        
        return result
        
    except Exception as e:
        logger.error(f"🔍 [PERPLEXITY] Search failed: {e}")
        raise RuntimeError(f"Perplexity search failed: {e}") from e


async def web_search(
    query: str,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> dict[str, Any]:
    """
    Tool-compatible web_search function that returns JSON-serializable result.
    
    This is the function that gets called when the LLM requests the web_search tool.
    
    Args:
        query: The search query
        model: Optional override for the model
        base_url: Optional override for base URL
        api_key: Optional override for API key
    
    Returns:
        Dict with normalized search result structure matching perplexity-search.md spec
    """
    result = await search_perplexity(
        query=query,
        model=model or "sonar-pro",
        base_url=base_url,
        api_key=api_key,
    )
    
    return {
        "query": result.query,
        "provider": result.provider,
        "model": result.model,
        "tookMs": result.took_ms,
        "content": result.content,
        "citations": result.citations,
    }
