import os
import logging
import time
from datetime import date, datetime, timezone
from urllib.parse import urlencode

from curl_cffi import requests

logger = logging.getLogger(__name__)

TRUTH_BASE = "https://truthsocial.com"
TRUTH_ACCOUNT = "realDonaldTrump"
WEB_UNLOCKER_URL = "https://api.brightdata.com/request"


def _api_key() -> str:
    key = os.environ.get("BRIGHTDATA_API_KEY", "")
    if not key:
        raise RuntimeError("BRIGHTDATA_API_KEY environment variable is not set")
    return key


def _unlock(url: str, params: dict | None = None) -> dict:
    """Fetch a URL through Bright Data Web Unlocker; returns parsed JSON."""
    full_url = url
    if params:
        full_url = f"{url}?{urlencode(params)}"

    resp = requests.post(
        WEB_UNLOCKER_URL,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {_api_key()}",
        },
        json={
            "zone": "web_unlocker1",
            "url": full_url,
            "format": "raw",
        },
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_truthsocial(target_date: date, backfill: bool = False) -> list[dict]:
    account_id = _ts_lookup(TRUTH_ACCOUNT)
    logger.info("TS: resolved @%s -> account_id=%s", TRUTH_ACCOUNT, account_id)

    if backfill:
        statuses = _ts_fetch_all_since(account_id, target_date)
    else:
        statuses = _ts_fetch_page(account_id)

    logger.info("TS: fetched %d statuses total", len(statuses))
    today_posts = _filter_by_date(statuses, target_date)
    logger.info("TS: %d posts match %s", len(today_posts), target_date.isoformat())
    return [_ts_to_output(s, account_id) for s in today_posts]


def _ts_lookup(handle: str) -> str:
    data = _unlock(
        f"{TRUTH_BASE}/api/v1/accounts/lookup",
        params={"acct": handle},
    )
    return data["id"]


def _ts_fetch_page(account_id: str, max_id: str | None = None) -> list[dict]:
    params = {}
    if max_id:
        params["max_id"] = max_id
    return _unlock(
        f"{TRUTH_BASE}/api/v1/accounts/{account_id}/statuses",
        params=params,
    )


def _ts_fetch_all_since(account_id: str, target_date: date) -> list[dict]:
    all_statuses: list[dict] = []
    max_id: str | None = None

    while True:
        page = _ts_fetch_page(account_id, max_id=max_id)
        if not page:
            break
        all_statuses.extend(page)
        oldest = page[-1]
        oldest_dt = datetime.fromisoformat(oldest["created_at"].replace("Z", "+00:00"))
        logger.info(
            "TS page: %d posts, oldest=%s (total: %d)",
            len(page),
            oldest_dt.date().isoformat(),
            len(all_statuses),
        )
        if oldest_dt.date() < target_date:
            break
        max_id = oldest["id"]
        if len(page) < 20:
            break
        time.sleep(2)

    return all_statuses


def _ts_to_output(status: dict, account_id: str) -> dict:
    return {
        "platform": "truthsocial",
        "post_id": status["id"],
        "content": status.get("content"),
        "url": status.get("url"),
        "replies_count": status.get("replies_count", 0),
        "reblogs_count": status.get("reblogs_count", 0),
        "favourites_count": status.get("favourites_count", 0),
        "upvotes_count": status.get("upvotes_count", 0),
        "media_attachments": status.get("media_attachments"),
        "tags": status.get("tags"),
        "mentions": status.get("mentions"),
        "created_at": status.get("created_at"),
        "crawled_at": datetime.now(timezone.utc).isoformat(),
        "raw_payload": status,
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
