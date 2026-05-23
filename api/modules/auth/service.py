import uuid

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.redis import get_redis
from core.security import (
    create_access_token,
    create_refresh_token,
    decode_access_token,
    hash_password,
    verify_password,
)
from models.user import User
from modules.auth.schemas import ChangePasswordRequest, RegisterRequest


async def register_user(db: AsyncSession, body: RegisterRequest) -> User:
    # Check if email already exists
    result = await db.execute(
        select(User).where(func.lower(User.email) == func.lower(body.email))
    )
    if result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already registered",
        )

    # Use email prefix if display_name is not provided
    display_name = body.display_name or body.email.split("@")[0]

    user = User(
        email=body.email,
        hashed_password=hash_password(body.password),
        display_name=display_name,
    )
    db.add(user)
    await db.flush()
    return user


async def authenticate_user(db: AsyncSession, email: str, password: str) -> User | None:
    result = await db.execute(
        select(User).where(func.lower(User.email) == func.lower(email))
    )
    user = result.scalar_one_or_none()

    if not user or not user.is_active:
        return None

    if not verify_password(password, user.hashed_password):
        return None

    return user


async def get_user_by_id(db: AsyncSession, user_id: uuid.UUID) -> User | None:
    return await db.get(User, user_id)


async def issue_tokens(user_id: uuid.UUID) -> tuple[str, str]:
    """Issue a new pair of access and refresh tokens."""
    access_token = create_access_token(subject=str(user_id))
    refresh_token, jti = create_refresh_token(subject=str(user_id))

    # Store refresh token jti in Redis as an allowlist
    redis = await get_redis()
    redis_key = f"refresh:{user_id}:{jti}"
    await redis.set(
        redis_key, 
        "1", 
        ex=settings.jwt_refresh_token_expire_minutes * 60 # Convert to seconds
    )

    return access_token, refresh_token


async def refresh_tokens(token: str) -> tuple[str, str]:
    """Rotate tokens using a valid refresh token."""
    try:
        payload = decode_access_token(token)
        if payload.get("type") != "refresh":
            raise ValueError("Invalid token type")
        
        user_id = payload.get("sub")
        jti = payload.get("jti")
        if not user_id or not jti:
            raise ValueError("Missing payload data")
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
        ) from exc

    # Check Redis for the jti
    redis = await get_redis()
    redis_key = f"refresh:{user_id}:{jti}"
    if not await redis.exists(redis_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token has been revoked",
        )

    # Delete the old token and issue new ones
    await redis.delete(redis_key)
    return await issue_tokens(uuid.UUID(user_id))


async def revoke_refresh_token(user_id: uuid.UUID, jti: str) -> None:
    """Revoke a specific refresh token."""
    redis = await get_redis()
    await redis.delete(f"refresh:{user_id}:{jti}")


async def revoke_all_user_tokens(user_id: uuid.UUID) -> None:
    """Revoke all active refresh tokens for a user (e.g. on password change)."""
    redis = await get_redis()
    keys = await redis.keys(f"refresh:{user_id}:*")
    if keys:
        await redis.delete(*keys)


async def change_password(
    db: AsyncSession, user: User, body: ChangePasswordRequest
) -> None:
    """Change user password and revoke all sessions."""
    if not verify_password(body.old_password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Incorrect current password",
        )

    user.hashed_password = hash_password(body.new_password)
    await db.flush()
    await revoke_all_user_tokens(user.id)
