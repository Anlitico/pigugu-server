# Trump Social Posts — Data Usage Guide

For developers who want to query Trump social media data produced by the Kubernetes CronJob crawler.

## Where the data comes from

A Kubernetes CronJob (`trump-crawler`) runs daily at 10:00 UTC (6:00 PM China time). It fetches the latest posts from both Truth Social and X/Twitter, then writes them directly into the project database via upsert.

- **K8s manifest**: `k8s/crawler-cronjob.yaml`
- **Crawler source**: `app/jobs/trump_social_crawler/`
- **Deployed alongside**: the API and agent via the same `Deploy to Amazon EKS` workflow

## Table: `trump_social_posts`

### Columns

| Column | Type | Description |
|---|---|---|
| `id` | UUID PK | Internal row ID |
| `platform` | `VARCHAR(20)` | `"truthsocial"` or `"x"` |
| `post_id` | `VARCHAR(255)` | Platform-native post ID |
| `content` | `TEXT` | Post body (HTML from Truth Social, HTML from RSS for X) |
| `url` | `VARCHAR(2048)` | Permalink to the original post |
| `created_at` | `TIMESTAMPTZ` | When the post was published on the platform |
| `crawled_at` | `TIMESTAMPTZ` | When our crawler fetched it |
| `replies_count` | `INTEGER` | Reply count (Truth Social only; `NULL` for X) |
| `reblogs_count` | `INTEGER` | Re-truth count (Truth Social only; `NULL` for X) |
| `favourites_count` | `INTEGER` | Like count (Truth Social only; `NULL` for X) |
| `upvotes_count` | `INTEGER` | Upvote count (Truth Social only; `NULL` for X) |
| `media_attachments` | `JSONB` | Images, videos, and metadata |
| `tags` | `JSONB` | Hashtag array |
| `mentions` | `JSONB` | Mentioned users array |
| `raw_payload` | `JSONB` | Full platform-native response (for debugging) |
| `inserted_at` | `TIMESTAMPTZ` | First time this row was written |
| `updated_at` | `TIMESTAMPTZ` | Last time engagement metrics were refreshed |

### Constraints

- **Unique**: `(platform, post_id)` — same post can't be inserted twice
- **Index** `(platform, created_at)` — efficient per-platform date queries
- **Index** `(created_at)` — efficient date-range queries

### X/Twitter caveats

X data comes from RSSHub community instances (not the official API). Engagement metrics (`replies_count`, `reblogs_count`, `favourites_count`, `upvotes_count`) are **always `NULL`** for X posts. Media, tags, and mentions may also be missing depending on what RSSHub exposes.

## How to query

### From a FastAPI route (recommended for HTTP endpoints)

Use the existing `get_db` dependency:

```python
from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.trump_social_post import TrumpSocialPost


@router.get("/trump-posts")
async def get_recent_posts(
    platform: str = "truthsocial",
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(TrumpSocialPost)
        .where(TrumpSocialPost.platform == platform)
        .order_by(TrumpSocialPost.created_at.desc())
        .limit(limit)
    )
    return result.scalars().all()
```

### From a standalone script or background job

Use `AsyncSessionLocal` directly:

```python
import asyncio
from sqlalchemy import select
from app.core.database import AsyncSessionLocal
from app.models.trump_social_post import TrumpSocialPost


async def main():
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(TrumpSocialPost)
            .order_by(TrumpSocialPost.created_at.desc())
            .limit(10)
        )
        for post in result.scalars():
            print(post.platform, post.url)


asyncio.run(main())
```

### Common queries

**Latest posts from one platform:**
```python
select(TrumpSocialPost).where(
    TrumpSocialPost.platform == "truthsocial"
).order_by(TrumpSocialPost.created_at.desc()).limit(20)
```

**Posts for a specific date:**
```python
from datetime import date

select(TrumpSocialPost).where(
    TrumpSocialPost.created_at >= date(2026, 5, 10),
    TrumpSocialPost.created_at < date(2026, 5, 11),
).order_by(TrumpSocialPost.created_at.desc())
```

**Top Truth Social posts by engagement:**
```python
select(TrumpSocialPost).where(
    TrumpSocialPost.platform == "truthsocial"
).order_by(TrumpSocialPost.favourites_count.desc()).limit(10)
```

**Search raw payload for a keyword (PostgreSQL JSONB):**
```python
from sqlalchemy import cast, String

select(TrumpSocialPost).where(
    cast(TrumpSocialPost.raw_payload, String).ilike("%keyword%")
)
```

## Data freshness

| Event | Time (UTC) | Time (China) |
|---|---|---|
| CronJob fires | 10:00 | 18:00 |
| Data lands in DB | ~10:00:30 | ~18:00:30 |

The crawler fetches the **latest page** of posts each run (no `--date` backfill). If you need historical data, run the crawler manually:

```bash
python -m app.jobs.trump_social_crawler --date 2026-05-10
```

This triggers backfill mode, which paginates backward until it covers the target date (Truth Social only; X RSS feed is limited to recent posts).

## Agent consumption

The LiveKit agent (`pigagent/main.py`) does not query the database directly. To make crawled posts available to the agent, add a FastAPI internal endpoint and have the agent call it via HTTP. This keeps database access logic inside the API container and avoids duplicating session management in the agent.

Example internal endpoint pattern:

```python
# app/modules/trump/router.py
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.models.trump_social_post import TrumpSocialPost

router = APIRouter(prefix="/internal/trump", tags=["trump-internal"])

@router.get("/recent")
async def recent_trump_posts(
    platform: str = Query("truthsocial"),
    limit: int = Query(10, le=50),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(TrumpSocialPost)
        .where(TrumpSocialPost.platform == platform)
        .order_by(TrumpSocialPost.created_at.desc())
        .limit(limit)
    )
    return result.scalars().all()
```

## Monitoring

Check that the CronJob is running:

```bash
kubectl get cronjobs
kubectl get jobs
kubectl logs job/trump-crawler-<id>
```

Check data landed in the database:

```sql
SELECT platform, COUNT(*), MAX(created_at)
FROM trump_social_posts
GROUP BY platform;
```
