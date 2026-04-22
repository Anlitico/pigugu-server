from datetime import datetime

from pydantic import BaseModel


class DebateStance(BaseModel):
    stance: str
    argument: str


class NewsItem(BaseModel):
    id: str
    title: str
    summary: str | None
    source_url: str | None
    category: str | None
    toxic_comment: str | None
    game_mode: str | None
    debate_stances: list[DebateStance] = []
    published_at: datetime | None

    model_config = {"from_attributes": True}


class NewsListResponse(BaseModel):
    items: list[NewsItem]
    total: int
