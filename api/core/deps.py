import uuid

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_db
from core.security import decode_access_token
from models.user import User
from modules.auth.service import get_user_by_id

# tokenUrl should match our login endpoint
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/v1/auth/login")


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = decode_access_token(token)
        user_id: str = payload.get("sub")
        if user_id is None:
            raise credentials_exception
    except ValueError:
        raise credentials_exception

    user = await get_user_by_id(db, uuid.UUID(user_id))
    if user is None:
        raise credentials_exception
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, 
            detail="Inactive user"
        )
    return user


async def get_current_user_ws(token: str) -> User | None:
    """Validate JWT token and return User, or None on failure.

    For WebSocket connections where we can't use Depends(oauth2_scheme).
    """
    try:
        payload = decode_access_token(token)
        user_id: str = payload.get("sub")
        if not user_id:
            return None
    except ValueError:
        return None

    from core.database import AsyncSessionLocal
    from modules.auth.service import get_user_by_id

    async with AsyncSessionLocal() as db:
        user = await get_user_by_id(db, uuid.UUID(user_id))
        if user is None or not user.is_active:
            return None
    return user
