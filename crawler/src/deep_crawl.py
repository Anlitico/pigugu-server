"""Deep crawl — enrich a topic with background, social sentiment, and context.

Phase 1: Google News search (free, reliable).
Phase 2: Add Reddit API, Twitter/X search, NewsAPI, etc.
"""

from __future__ import annotations

from loguru import logger
import re
from urllib.parse import quote_plus

import feedparser
import httpx


# ── Google News search for topic enrichment ───────────────────────────────────

GOOGLE_NEWS_SEARCH = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"


async def deep_crawl_topic(topic: str, headline: str = "") -> dict:
    """Enrich a topic with broader news coverage and context.

    Args:
        topic: Short topic descriptor (e.g. "Trump golf course renovation")
        headline: Original article headline for context

    Returns:
        {
            "topic": str,
            "articles": [{title, source, url, snippet}],  # related news
            "background": str,   # aggregated background context
            "angles": [str],     # potential roast angles found
            "social_sentiment": str,  # placeholder for Phase 2 social media
        }
    """
    result = {
        "topic": topic,
        "headline": headline,
        "articles": [],
        "background": "",
        "angles": [],
        "social_sentiment": "",
    }

    try:
        # Search for topic on Google News
        query = quote_plus(topic[:200])
        url = GOOGLE_NEWS_SEARCH.format(query=query)

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url, follow_redirects=True)
            resp.raise_for_status()
            feed = feedparser.parse(resp.text)

        related = []
        for entry in feed.entries[:15]:
            related.append({
                "title": _clean(entry.get("title", ""))[:200],
                "source": entry.get("source", {}).get("title", "") if isinstance(entry.get("source"), dict) else "",
                "url": entry.get("link", ""),
                "snippet": _clean(entry.get("summary", ""))[:300],
            })

        result["articles"] = related

        # Build aggregated background from related articles
        if related:
            snippets = [a["snippet"] for a in related if a["snippet"]]
            result["background"] = " | ".join(snippets[:5])[:2000]

        # Extract potential roast angles from headlines
        for a in related:
            angle = _extract_angle(a["title"])
            if angle:
                result["angles"].append(angle)

        logger.info("Deep crawl for '%s': %d related articles, %d angles",
                     topic[:60], len(related), len(result["angles"]))

    except httpx.HTTPError as exc:
        logger.warning("Deep crawl HTTP error for '%s': %s", topic[:60], exc)
    except Exception:
        logger.exception("Deep crawl failed for '%s'", topic[:60])

    return result


def _extract_angle(title: str) -> str | None:
    """Try to extract a roastable angle from a headline."""
    # Look for controversy markers
    markers = [
        r"backlash",
        r"outrage",
        r"controversy",
        r"scandal",
        r"furious",
        r"slam",
        r"blasted",
        r"under fire",
        r"criticized",
        r"accused",
        r"admits",
        r"apologizes",
        r"walked back",
    ]
    title_lower = title.lower()
    if any(re.search(m, title_lower) for m in markers):
        return title[:200]
    return None


def _clean(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", str(text)).strip()
