from sqlalchemy.ext.asyncio import AsyncSession

from app.models.news import News


async def list_news(
    db: AsyncSession, category: str | None, page: int, page_size: int
) -> tuple[list[News], int]:
    ...


async def get_news_by_id(db: AsyncSession, news_id: str) -> News | None:
    ...


async def fetch_and_process_news() -> None:
    """Fetch RSS/NewsAPI, call Claude to generate toxic_comment/stances/mood, persist to DB + Redis."""
    ...
