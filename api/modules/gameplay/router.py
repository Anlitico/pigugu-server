from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_db
from modules.gameplay.schemas import ScenarioCard, ScenarioDetail, ScenarioListResponse
from modules.gameplay.service import get_scenario_detail, list_active_scenarios

router = APIRouter(prefix="/gameplay", tags=["gameplay"])


@router.get("/scenarios", response_model=ScenarioListResponse)
async def list_scenarios(
    mode: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    scenarios = await list_active_scenarios(db, mode)
    return ScenarioListResponse(
        scenarios=[ScenarioCard(**s) for s in scenarios]
    )


@router.get("/scenarios/{roast_id}", response_model=ScenarioDetail)
async def get_scenario(
    roast_id: str,
    db: AsyncSession = Depends(get_db),
):
    detail = await get_scenario_detail(db, roast_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Scenario not found")
    return ScenarioDetail(**detail)
