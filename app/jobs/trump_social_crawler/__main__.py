import argparse
import asyncio
import logging
from datetime import date

from app.jobs.trump_social_crawler.fetch_truthsocial import fetch_truthsocial
from app.jobs.trump_social_crawler.fetch_x import fetch_x
from app.jobs.trump_social_crawler.repository import upsert_posts

logger = logging.getLogger("trumpcrawler")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")


async def main_async() -> None:
    parser = argparse.ArgumentParser(description="Crawl Trump social media posts")
    parser.add_argument(
        "--platform",
        choices=["truthsocial", "x", "all"],
        default="all",
        help="Which platform to crawl (default: all)",
    )
    parser.add_argument(
        "--date",
        type=str,
        help="Target date in YYYY-MM-DD format (default: today)",
    )
    args = parser.parse_args()

    target_date = date.fromisoformat(args.date) if args.date else date.today()
    backfill = args.date is not None
    logger.info(
        "Crawling for %s (backfill=%s, platform=%s)",
        target_date.isoformat(),
        backfill,
        args.platform,
    )

    if args.platform in ("truthsocial", "all"):
        try:
            posts = fetch_truthsocial(target_date, backfill=backfill)
            count = await upsert_posts(posts)
            logger.info("TS: upserted %d posts", count)
        except Exception:
            logger.exception("TS: crawl failed")

    if args.platform in ("x", "all"):
        try:
            posts = fetch_x(target_date, backfill=backfill)
            count = await upsert_posts(posts)
            logger.info("X: upserted %d posts", count)
        except Exception:
            logger.exception("X: crawl failed")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
