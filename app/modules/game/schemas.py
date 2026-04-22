from datetime import datetime

from pydantic import BaseModel


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
