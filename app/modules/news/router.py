from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.modules.news.schemas import NewsItem, NewsListResponse

router = APIRouter(prefix="/news", tags=["news"])


@router.get("/feed", response_model=NewsListResponse)
async def list_news(
    category: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    ...


@router.get("/{news_id}", response_model=NewsItem)
async def get_news(news_id: str, db: AsyncSession = Depends(get_db)):
    ...
