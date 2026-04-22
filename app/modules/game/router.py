from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.modules.game.schemas import ConversationSummary, GameStateResponse, LeaderboardResponse

router = APIRouter(prefix="/user", tags=["game"])


@router.get("/game-state", response_model=GameStateResponse)
async def get_game_state(db: AsyncSession = Depends(get_db)):
    ...


@router.get("/achievements")
async def get_achievements(db: AsyncSession = Depends(get_db)):
    ...


@router.get("/conversations", response_model=list[ConversationSummary])
async def list_conversations(db: AsyncSession = Depends(get_db)):
    ...


leaderboard_router = APIRouter(prefix="/leaderboard", tags=["game"])


@leaderboard_router.get("/daily", response_model=LeaderboardResponse)
async def daily_leaderboard(db: AsyncSession = Depends(get_db)):
    ...
