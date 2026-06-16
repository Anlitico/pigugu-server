from datetime import datetime

from pydantic import BaseModel


class RoastHistoryResponse(BaseModel):
    roast_instance_id: str
    roast_id: str = ""
    mode: str = ""
    headline: str = ""
    source: str = ""
    turn_count: int = 0
    best_take: str | None = None
    interrupted: bool = False
    started_at: str | None = None
    settled_at: str | None = None


class PendingRoastItem(BaseModel):
    roast_instance_id: str
    roast_id: str = ""
    mode: str = ""
    headline: str = ""
    source: str = ""
    turn_count: int = 0
    best_take: str | None = None
    interrupted: bool = False
    started_at: str | None = None
    settled_at: str | None = None


class PendingRoastsResponse(BaseModel):
    roasts: list[PendingRoastItem]


class AchievementResponse(BaseModel):
    id: str
    code: str
    label: str
    earned_at: datetime

    model_config = {"from_attributes": True}


class ConversationSummary(BaseModel):
    id: str
    outcome: str | None
    score_delta: int | None
    duration_seconds: int | None
    summary: str | None
    started_at: datetime

    model_config = {"from_attributes": True}


class GameStateResponse(BaseModel):
    credibility_score: int
    title: str
    today_conversations: int
    total_conversations: int
    achievements: list[AchievementResponse]


class LeaderboardEntry(BaseModel):
    rank: int
    user_id: str
    display_name: str | None
    score: int


class LeaderboardResponse(BaseModel):
    entries: list[LeaderboardEntry]
    my_rank: int | None
