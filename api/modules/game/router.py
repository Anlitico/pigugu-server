from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_db
from core.deps import get_current_user
from models.user import User
from modules.game import service
from modules.game.schemas import RoastResultResponse, PendingRoastsResponse

router = APIRouter(prefix="/user", tags=["game"])


@router.get("/roast-result/{roast_instance_id}", response_model=RoastResultResponse)
async def get_roast_result(
    roast_instance_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get settlement data for a completed roast."""
    result = await service.get_roast_result(db, current_user.id, roast_instance_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Roast result not found")
    return RoastResultResponse(**result)


@router.get("/pending-roasts", response_model=PendingRoastsResponse)
async def get_pending_roasts(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get unviewed roast results for the current user."""
    roasts = await service.get_pending_roasts(db, current_user.id)
    return PendingRoastsResponse(roasts=roasts)


@router.post("/roast-result/{roast_instance_id}/viewed")
async def mark_roast_viewed(
    roast_instance_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Mark a roast result as viewed."""
    await service.mark_roast_viewed(db, current_user.id, roast_instance_id)
    return {"ok": True}


@router.get("/game-state")
async def get_game_state(current_user: User = Depends(get_current_user),
                         db: AsyncSession = Depends(get_db)):
    ...


@router.get("/achievements")
async def get_achievements(current_user: User = Depends(get_current_user),
                           db: AsyncSession = Depends(get_db)):
    ...


@router.get("/conversations")
async def list_conversations(current_user: User = Depends(get_current_user),
                              db: AsyncSession = Depends(get_db)):
    ...


leaderboard_router = APIRouter(prefix="/leaderboard", tags=["game"])


@leaderboard_router.get("/daily")
async def daily_leaderboard(db: AsyncSession = Depends(get_db)):
    ...

