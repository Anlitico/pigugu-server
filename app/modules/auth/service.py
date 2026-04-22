import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.modules.auth.schemas import RegisterRequest


async def register_user(db: AsyncSession, body: RegisterRequest) -> User:
    ...


async def authenticate_user(db: AsyncSession, email: str, password: str) -> User | None:
    ...


async def get_user_by_id(db: AsyncSession, user_id: uuid.UUID) -> User | None:
    ...
