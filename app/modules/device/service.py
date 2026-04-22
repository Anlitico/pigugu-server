import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device import Device
from app.modules.device.schemas import DeviceBindRequest


async def bind_device(db: AsyncSession, user_id: uuid.UUID, body: DeviceBindRequest) -> Device:
    ...


async def get_devices_for_user(db: AsyncSession, user_id: uuid.UUID) -> list[Device]:
    ...


async def rename_device(db: AsyncSession, device_id: uuid.UUID, name: str) -> Device:
    ...


async def unbind_device(db: AsyncSession, device_id: uuid.UUID) -> None:
    ...


async def generate_livekit_token(device_id: uuid.UUID) -> tuple[str, str]:
    """Returns (token, room_name)."""
    ...


async def update_device_state(device_id: str, state: str) -> None:
    """Write state to Redis with 60s TTL."""
    ...
