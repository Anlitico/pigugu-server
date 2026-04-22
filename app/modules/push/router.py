from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.modules.push.schemas import FCMTokenRegisterRequest

router = APIRouter(prefix="/push", tags=["push"])


@router.post("/token", status_code=201)
async def register_fcm_token(body: FCMTokenRegisterRequest, db: AsyncSession = Depends(get_db)):
    ...
