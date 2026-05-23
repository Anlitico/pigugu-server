import logging
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone

from curl_cffi import requests

logger = logging.getLogger(__name__)

X_RSSHUBS = [
    "https://rsshub.pseudoyu.com/twitter/user/realDonaldTrump",
    "https://rsshub.rssforever.com/twitter/user/realDonaldTrump",
]


def fetch_x(target_date: date, backfill: bool = False) -> list[dict]:
    rss_text = _x_fetch_rss()
    items = _x_parse_rss(rss_text)
    logger.info("X: parsed %d tweets from RSS", len(items))
    today_posts = _filter_by_date(items, target_date)
    logger.info("X: %d tweets match %s", len(today_posts), target_date.isoformat())
    return [_x_to_output(t) for t in today_posts]


def _x_fetch_rss() -> str:
    for url in X_RSSHUBS:
        try:
            resp = requests.get(url, impersonate="chrome110", timeout=15)
            resp.raise_for_status()
            if "rss" in resp.text[:500].lower() or "<item>" in resp.text[:500]:
                logger.info("X: fetched RSS from %s (%d bytes)", url, len(resp.text))
                return resp.text
        except Exception as e:
            logger.warning("X: RSSHub %s failed: %s", url, e)
            continue
    raise RuntimeError("All X RSSHub instances failed")


def _x_parse_rss(rss_text: str) -> list[dict]:
    root = ET.fromstring(rss_text)
    items = []

    for item in root.findall(".//item"):
        link = item.findtext("link", "")
        tweet_id = link.rstrip("/").split("/")[-1] if link else ""

        pubdate = item.findtext("pubDate", "")
        created_at = ""
        if pubdate:
            try:
                created_at = (
                    datetime.strptime(pubdate, "%a, %d %b %Y %H:%M:%S %Z")
                    .replace(tzinfo=timezone.utc)
                    .isoformat()
                )
            except ValueError:
                created_at = pubdate

        description = item.findtext("description", "") or ""
        content = description.strip()

        items.append({
            "post_id": tweet_id,
            "content": content,
            "url": link,
            "created_at": created_at,
        })

    return items


def _x_to_output(tweet: dict) -> dict:
    return {
        "platform": "x",
        "post_id": tweet["post_id"],
        "content": tweet["content"],
        "url": tweet["url"],
        "created_at": tweet["created_at"],
        "crawled_at": datetime.now(timezone.utc).isoformat(),
    }


def _filter_by_date(posts: list[dict], target_date: date) -> list[dict]:
    result = []
    for p in posts:
        created_at = p.get("created_at", "")
        try:
            post_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            logger.warning("Could not parse date: %s", created_at)
            continue
        if post_dt.date() == target_date:
            result.append(p)
    return result
