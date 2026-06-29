"""Fetch headlines from AP and Reuters via RSS feeds.

Supports:
- Single-day fetch (for testing)
- 7-day rolling window (for production pipeline)
- Domain classification via keyword matching
"""

from __future__ import annotations

import hashlib
from loguru import logger
import re
from datetime import datetime, timedelta, timezone
from typing import TypedDict

import feedparser
import httpx

try:
    from .domain_weights import get_domain_weight
except ImportError:
    from domain_weights import get_domain_weight  # type: ignore[no-redef]


# ── RSS endpoints ───────────────────────────────────────────────────────────

AP_RSS_URL = (
    "https://news.google.com/rss/search"
    "?q=site:apnews.com&hl=en-US&gl=US&ceid=US:en"
)

REUTERS_RSS_URL = (
    "https://news.google.com/rss/search"
    "?q=Reuters+news&hl=en-US&gl=US&ceid=US:en"
)

# ── Domain classification keyword map ──────────────────────────────────────

DOMAIN_KEYWORDS: dict[str, list[str]] = {
    "Politics": [
        "trump", "congress", "supreme court", "democrat", "republican", "biden",
        "white house", "senate", "governor", "election", "vote", "bill ", "lawmaker",
        "capitol", "federal", "birthright", "citizenship", "legislation",
        "president", "administration", "campaign", "govern",
    ],
    "Economy": [
        "stock", "dollar", "gold ", "rate", "inflation", "fed ", "bank",
        "mortgage", "bond", "market", "yield", "invest", "lending",
        "tariff", "trade war", "recession", "debt", "budget", "economic",
        "jpmorgan", "wall street", "s&p", "nasdaq", "dow ",
    ],
    "Tech": [
        "ai ", "artificial intelligence", "chip", "nvidia", "samsung",
        "anthropic", "semiconductor", "data center", "robot", "automation",
        "bitcoin", "crypto", "tech ", "silicon", "hynix", "baidu",
        "openai", "google ", "apple ", "microsoft", "meta ", "amazon ",
        "startup", "app ", "software", "cyber",
    ],
    "Business": [
        "ceo", "merger", "acquisition", "layoff", "spin-off", "bankrupt",
        "sale", "revenue", "profit", "business", "company", "corporate",
        "antitrust", "ipo", "shareholder", "board", "executive", "strike",
        "lawsuit", "fine ", "settlement",
    ],
    "Social": [
        "pride", "lgbtq", "lgbt", "gay ", "trans", "culture war",
        "social media", "tiktok", "instagram", "influencer", "viral",
        "protest", "activist", "boycott", "cancel ", "woke", "diversity",
    ],
    "Health": [
        "drug", "pharma", "fda", "hospital", "health", "disease",
        "cancer", "biotech", "gene ", "medical", "vaccine", "epidemic",
        "mental health", "therapy", "medicare", "medicaid",
    ],
    "Climate": [
        "climate", "heatwave", "heat wave", "wildfire", "hurricane",
        "flood", "drought", "carbon", "emission", "green", "renewable",
        "earthquake", "disaster", "storm", "environment", "pollution",
    ],
    "International": [
        "ukraine", "russia", "china ", "iran", "israel", "gaza",
        "nato", "europe", "asia", "africa", "middle east", "war ",
        "sanction", "diplomat", "treaty", "un ", "peace",
    ],
    "Sports": [
        "world cup", "nfl", "nba", "mlb", "nascar", "golf", "tennis",
        "wimbledon", "soccer", "football", "playoff", "championship",
        "olympic", "tournament", "upset", "score", "coach", "transfer",
    ],
    "Entertainment": [
        "movie", "film ", "tv ", "award", "celebrity", "actor",
        "streaming", "netflix", "disney", "hbo", "concert", "album",
        "box office", "trailer", "star wars", "marvel",
    ],
    "Immigration": [
        "immigra", "migrant", "border", "asylum", "visa", "refugee",
        "deport", "ice ", "daca", "green card",
    ],
    "Housing": [
        "housing", "rent ", "home price", "property", "real estate",
        "mortgage rate", "landlord", "eviction", "affordable housing",
        "homeless", "zoning",
    ],
    "Science": [
        "nasa", "space", "telescope", "science", "research", "discover",
        "dinosaur", "fossil", "planet", "mars ", "moon ", "physics",
        "quantum", "dna ", "species",
    ],
}


class ArticleDict(TypedDict, total=False):
    source: str
    article_id: str
    title: str
    summary: str
    url: str
    category: str
    domain: str
    published_at: str


# ── Public API ───────────────────────────────────────────────────────────────


async def fetch_week_articles() -> list[ArticleDict]:
    """Fetch 7 days of headlines from AP + Reuters, classified by domain.

    Returns articles with an added 'domain' field.
    """
    ap = await _fetch_days(AP_RSS_URL, "ap", days=7)
    reuters = await _fetch_days(REUTERS_RSS_URL, "reuters", days=7)
    articles = ap + reuters

    # Classify each article into a domain
    for a in articles:
        a["domain"] = _classify_domain(a["title"], a.get("summary", ""))

    logger.info("Fetched %d articles (AP=%d, Reuters=%d) over 7 days",
                len(articles), len(ap), len(reuters))
    return articles


async def fetch_ap(date_str: str | None = None) -> list[ArticleDict]:
    url = _day_filter(AP_RSS_URL, date_str)
    return await _fetch("ap", url)


async def fetch_reuters(date_str: str | None = None) -> list[ArticleDict]:
    url = _day_filter(REUTERS_RSS_URL, date_str)
    return await _fetch("reuters", url)


# ── Internals ────────────────────────────────────────────────────────────────


async def _fetch_days(base_url: str, source: str, days: int) -> list[ArticleDict]:
    """Fetch from multiple days to build a 1-week corpus."""
    all_articles: list[ArticleDict] = []
    seen: set[str] = set()
    today = datetime.now(timezone.utc).date()

    for offset in range(days):
        d = today - timedelta(days=offset)
        url = _day_filter(base_url, d.isoformat())
        batch = await _fetch(source, url)
        for a in batch:
            if a["article_id"] not in seen:
                seen.add(a["article_id"])
                all_articles.append(a)

    return all_articles


async def _fetch(source: str, url: str) -> list[ArticleDict]:
    """Fetch one RSS feed and return standardised article dicts."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url, follow_redirects=True)
            resp.raise_for_status()
            feed = feedparser.parse(resp.text)
    except httpx.HTTPError as exc:
        logger.error("%s RSS fetch failed: %s", source.upper(), exc)
        return []
    except Exception:
        logger.exception("%s RSS parse failed", source.upper())
        return []

    if feed.bozo:
        logger.warning("%s RSS may be malformed: %s", source.upper(), feed.bozo_exception)

    articles: list[ArticleDict] = []
    for entry in feed.entries:
        title = _clean(entry.get("title", ""))
        if not title or len(title) < 10:
            continue
        article_id = _make_id(source, entry)
        published = _parse_published(entry)
        articles.append(
            ArticleDict(
                source=source,
                article_id=article_id,
                title=title,
                summary=_clean(entry.get("summary", entry.get("description", ""))),
                url=entry.get("link", ""),
                category="",
                domain="",
                published_at=published.isoformat() if published else "",
            )
        )

    logger.info("%s: %d articles fetched", source.upper(), len(articles))
    return articles


def _classify_domain(title: str, summary: str) -> str:
    """Classify an article into a domain via keyword matching."""
    text = (title + " " + summary).lower()
    scores: dict[str, int] = {}
    for domain, keywords in DOMAIN_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in text)
        if score > 0:
            scores[domain] = score
    if not scores:
        return "Other"
    # Return highest-scoring domain
    return max(scores, key=lambda d: (scores[d], get_domain_weight(d)))


def _make_id(source: str, entry) -> str:
    guid = entry.get("guid") or entry.get("id") or ""
    if guid and guid.strip():
        return f"{source}_{hashlib.sha256(guid.strip().encode()).hexdigest()[:16]}"
    link = entry.get("link", "")
    return f"{source}_{hashlib.sha256(link.encode()).hexdigest()[:16]}"


def _parse_published(entry) -> datetime | None:
    struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if struct:
        try:
            return datetime(*struct[:6], tzinfo=timezone.utc)
        except (TypeError, ValueError):
            pass
    raw = entry.get("published") or entry.get("updated", "")
    if raw:
        try:
            from feedparser import _parse_date as parse_date
            parsed = parse_date(raw)
            if parsed:
                return datetime(*parsed[:6], tzinfo=timezone.utc)
        except Exception:
            pass
    return None


def _clean(text: str, max_len: int = 2000) -> str:
    clean = re.sub(r"<[^>]+>", " ", str(text))
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:max_len]


def _day_filter(url: str, date_str: str | None) -> str:
    """Apply a date filter to Google News RSS URL."""
    if date_str:
        return url.replace("when:1d", f"when:{date_str}")
    return url
