from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.modules.device.schemas import (
    DeviceBindRequest,
    DeviceRenameRequest,
    DeviceResponse,
    DeviceStateRequest,
    LiveKitTokenResponse,
)

router = APIRouter(prefix="/device", tags=["device"])


@router.post("/bind", response_model=DeviceResponse, status_code=201)
async def bind_device(body: DeviceBindRequest, db: AsyncSession = Depends(get_db)):
    ...


@router.post("/state")
async def report_state(body: DeviceStateRequest, db: AsyncSession = Depends(get_db)):
    ...


@router.get("/livekit-token", response_model=LiveKitTokenResponse)
async def get_livekit_token(device_id: str, db: AsyncSession = Depends(get_db)):
    ...


@router.get("s", response_model=list[DeviceResponse])
async def list_devices(db: AsyncSession = Depends(get_db)):
    ...


@router.patch("/{device_id}/name", response_model=DeviceResponse)
async def rename_device(device_id: str, body: DeviceRenameRequest, db: AsyncSession = Depends(get_db)):
    ...


@router.delete("/{device_id}", status_code=204)
async def unbind_device(device_id: str, db: AsyncSession = Depends(get_db)):
    ...
