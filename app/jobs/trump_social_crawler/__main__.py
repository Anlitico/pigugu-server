import argparse
import asyncio
import logging
from datetime import date

from app.jobs.trump_social_crawler.classifier import classify_and_store
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
    parser.add_argument(
        "--skip-classify",
        action="store_true",
        help="Skip gameplay classification after crawl",
    )
    args = parser.parse_args()

    target_date = date.fromisoformat(args.date) if args.date else date.today()
    backfill = args.date is not None
    logger.info(
        "Crawling for %s (backfill=%s, platform=%s, classify=%s)",
        target_date.isoformat(),
        backfill,
        args.platform,
        not args.skip_classify,
    )

    if args.platform in ("truthsocial", "all"):
        await _crawl_and_classify(
            "truthsocial", target_date, backfill, args.skip_classify
        )

    if args.platform in ("x", "all"):
        await _crawl_and_classify("x", target_date, backfill, args.skip_classify)


async def _crawl_and_classify(
    platform: str,
    target_date: date,
    backfill: bool,
    skip_classify: bool,
) -> None:
    fetch = fetch_truthsocial if platform == "truthsocial" else fetch_x
    label = "TS" if platform == "truthsocial" else "X"

    try:
        posts = fetch(target_date, backfill=backfill)
        result = await upsert_posts(posts)
        logger.info(
            "%s: %d inserted, %d updated, %d total",
            label,
            result["inserted"],
            result["updated"],
            result["total"],
        )

        if not skip_classify and result["new_posts"]:
            logger.info(
                "%s: classifying %d new posts...",
                label,
                len(result["new_posts"]),
            )
            for post in result["new_posts"]:
                try:
                    await classify_and_store(post)
                except Exception:
                    logger.exception(
                        "%s: classify failed for post %s",
                        label,
                        post.get("post_id"),
                    )
        elif skip_classify:
            logger.info("%s: classification skipped (--skip-classify)", label)
        else:
            logger.info("%s: no new posts to classify", label)

    except Exception:
        logger.exception("%s: crawl failed", label)


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
