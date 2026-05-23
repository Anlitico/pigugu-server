from datetime import datetime

from pydantic import BaseModel, computed_field


class ScenarioCard(BaseModel):
    roast_id: str
    game_mode: str
    headline: str
    source: str
    source_url: str
    teaser: str
    tags: list[str]
    is_urgent: bool
    created_at: datetime
    expires_at: datetime | None

    model_config = {"from_attributes": True}

    @computed_field
    @property
    def is_live(self) -> bool:
        return (
            self.game_mode == "breaking_bomb"
            and self.expires_at is not None
            and self.expires_at > datetime.now(tz=self.expires_at.tzinfo)
        )


class ScenarioDetail(ScenarioCard):
    prompt: str


class ScenarioListResponse(BaseModel):
    scenarios: list[ScenarioCard]
