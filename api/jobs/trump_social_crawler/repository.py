import logging
from datetime import datetime, timezone

from sqlalchemy import select

from core.database import AsyncSessionLocal
from models.trump_social_post import TrumpSocialPost

logger = logging.getLogger(__name__)


async def upsert_posts(posts: list[dict]) -> dict:
    """Upsert posts into trump_social_posts.

    Returns:
        {"total": int, "inserted": int, "updated": int,
         "new_posts": list[dict]}  — new_posts includes the DB-assigned id
    """
    if not posts:
        return {"total": 0, "inserted": 0, "updated": 0, "new_posts": []}

    async with AsyncSessionLocal() as session:
        inserted = 0
        updated = 0
        new_posts: list[dict] = []

        for p in posts:
            post_id = p["post_id"]
            platform = p["platform"]

            result = await session.execute(
                select(TrumpSocialPost).where(
                    TrumpSocialPost.platform == platform,
                    TrumpSocialPost.post_id == post_id,
                )
            )
            existing = result.scalar_one_or_none()

            if existing:
                existing.content = p.get("content")
                existing.url = p.get("url")
                existing.created_at = _parse_dt(p.get("created_at"))
                existing.crawled_at = _parse_dt(p["crawled_at"])
                existing.replies_count = p.get("replies_count")
                existing.reblogs_count = p.get("reblogs_count")
                existing.favourites_count = p.get("favourites_count")
                existing.upvotes_count = p.get("upvotes_count")
                existing.media_attachments = p.get("media_attachments")
                existing.tags = p.get("tags")
                existing.mentions = p.get("mentions")
                existing.raw_payload = p.get("raw_payload")
                updated += 1
            else:
                row = TrumpSocialPost(
                    platform=platform,
                    post_id=post_id,
                    content=p.get("content"),
                    url=p.get("url"),
                    created_at=_parse_dt(p.get("created_at")),
                    crawled_at=_parse_dt(p["crawled_at"]),
                    replies_count=p.get("replies_count"),
                    reblogs_count=p.get("reblogs_count"),
                    favourites_count=p.get("favourites_count"),
                    upvotes_count=p.get("upvotes_count"),
                    media_attachments=p.get("media_attachments"),
                    tags=p.get("tags"),
                    mentions=p.get("mentions"),
                    raw_payload=p.get("raw_payload"),
                )
                session.add(row)
                await session.flush()  # assign row.id before commit
                inserted += 1
                # Build dict with DB-assigned id for downstream classifier
                post_with_id = dict(p)
                post_with_id["id"] = str(row.id)
                new_posts.append(post_with_id)

        await session.commit()
        logger.info("DB: %d inserted, %d updated", inserted, updated)
        return {
            "total": inserted + updated,
            "inserted": inserted,
            "updated": updated,
            "new_posts": new_posts,
        }


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
