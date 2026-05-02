# User Authentication — Server Implementation Spec

## 1. Database Schema

### Table: `users`

> Already defined in `app/models/user.py`. Migration needs to be generated.

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | `UUID` | PK, default `uuid4` | |
| `email` | `VARCHAR(255)` | UNIQUE, NOT NULL, INDEX | Case-insensitive lookup needed |
| `hashed_password` | `VARCHAR(255)` | NOT NULL | bcrypt hash |
| `display_name` | `VARCHAR(100)` | NULLABLE | Defaults to email prefix if null |
| `is_active` | `BOOLEAN` | NOT NULL, default `true` | Soft-disable account |
| `created_at` | `TIMESTAMPTZ` | NOT NULL, server default `now()` | |
| `updated_at` | `TIMESTAMPTZ` | NOT NULL, server default `now()`, on update `now()` | |

### Redis Key Schema

| Key Pattern | Value | TTL | Purpose |
|-------------|-------|-----|---------|
| `refresh:{user_id}:{jti}` | `"1"` | 30 days | Refresh token allowlist |

- `user_id`: UUID as string
- `jti`: UUID v4, unique per token issuance (`jwt_id` claim in the token payload)

---

## 2. Configuration

### New env vars to add (`.env.example` and `config.py`)

```ini
JWT_REFRESH_TOKEN_EXPIRE_DAYS=30
```

### `app/core/config.py` addition

```python
jwt_refresh_token_expire_days: int = 30
```

---

## 3. Security Module (`app/core/security.py`)

### Existing functions (keep as-is)

- `hash_password(plain: str) -> str`
- `verify_password(plain: str, hashed: str) -> bool`
- `create_access_token(subject: str, extra: dict | None = None) -> str`
- `decode_access_token(token: str) -> dict`

### New function to add

```python
def create_refresh_token(subject: str) -> tuple[str, str]:
    """
    Returns (token_string, jti).
    jti is stored as the Redis key suffix for revocation.
    """
    jti = str(uuid.uuid4())
    expire = datetime.now(timezone.utc) + timedelta(days=settings.jwt_refresh_token_expire_days)
    payload = {"sub": subject, "exp": expire, "jti": jti, "type": "refresh"}
    token = jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    return token, jti
```

---

## 4. Auth Dependency (`app/core/deps.py`) — NEW FILE

This file provides FastAPI dependencies for protected routes.

```python
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import decode_access_token
from app.models.user import User
from app.modules.auth.service import get_user_by_id

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/v1/auth/login")

async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """
    Validates the Bearer access token and returns the active User.
    Raises 401 if token is invalid or expired.
    Raises 403 if user is inactive.
    """
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
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Inactive user")
    return user
```

Usage in any protected router:
```python
from app.core.deps import get_current_user

@router.get("/me")
async def get_me(current_user: User = Depends(get_current_user)):
    ...
```

---

## 5. Schemas (`app/modules/auth/schemas.py`)

### Existing (keep as-is)

```python
class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    display_name: str | None = None   # optional

class LoginRequest(BaseModel):
    email: EmailStr
    password: str

class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"

class RefreshRequest(BaseModel):
    refresh_token: str

class UserResponse(BaseModel):
    id: str
    email: str
    display_name: str | None
    model_config = {"from_attributes": True}
```

### New schema to add

```python
class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str
```

### Password validation rule
- `new_password`: minimum 8 characters. Enforce via `field_validator`.

```python
from pydantic import field_validator

class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def password_min_length(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v
```

---

## 6. Service Layer (`app/modules/auth/service.py`)

### Function Specifications

#### `register_user(db, body) -> User`

```
Input:  RegisterRequest (email, password, display_name?)
Output: User (newly created)
Errors: 409 Conflict if email already exists
```

Logic:
1. Query `users` table by `email` (case-insensitive: `lower(email) = lower(:email)`)
2. If found → raise `HTTPException(409, "Email already registered")`
3. `hashed_password = hash_password(body.password)`
4. `display_name = body.display_name or body.email.split("@")[0]`
5. Create `User` instance, `db.add(user)`, `await db.flush()` (to get the ID)
6. Return `user`

---

#### `authenticate_user(db, email, password) -> User | None`

```
Input:  email (str), password (str)
Output: User if credentials valid, None otherwise
```

Logic:
1. Query user by `lower(email) = lower(:email)`
2. If not found → return `None`
3. If `user.is_active == False` → return `None`
4. `verify_password(password, user.hashed_password)` → if False → return `None`
5. Return `user`

---

#### `get_user_by_id(db, user_id) -> User | None`

```
Input:  user_id (UUID)
Output: User or None
```

Logic: `await db.get(User, user_id)`

---

#### `issue_tokens(user_id: str) -> tuple[str, str, str]`

```
Input:  user_id as string
Output: (access_token, refresh_token, jti)
```

Logic:
1. `access_token = create_access_token(subject=user_id)`
2. `refresh_token, jti = create_refresh_token(subject=user_id)`
3. Store in Redis: `await redis.set(f"refresh:{user_id}:{jti}", "1", ex=30*86400)`
4. Return `(access_token, refresh_token, jti)`

---

#### `refresh_tokens(refresh_token: str) -> tuple[str, str]`

```
Input:  refresh_token (JWT string)
Output: (new_access_token, new_refresh_token)
Errors: 401 if token invalid / expired / revoked
```

Logic:
1. `payload = decode_access_token(refresh_token)` → catch ValueError → 401
2. Check `payload.get("type") == "refresh"` → else 401
3. `jti = payload["jti"]`, `user_id = payload["sub"]`
4. Check Redis: `await redis.exists(f"refresh:{user_id}:{jti}")` → if 0 → 401 (revoked)
5. Delete old key: `await redis.delete(f"refresh:{user_id}:{jti}")`
6. Call `issue_tokens(user_id)` to generate a new pair
7. Return `(new_access_token, new_refresh_token)`

---

#### `revoke_refresh_token(user_id: str, jti: str) -> None`

```
Input:  user_id (str), jti (str)
```

Logic: `await redis.delete(f"refresh:{user_id}:{jti}")`

---

#### `revoke_all_user_tokens(user_id: str) -> None`

```
Input:  user_id (str)
Used by: change_password
```

Logic: `await redis.delete(*await redis.keys(f"refresh:{user_id}:*"))`

> [!NOTE]
> `redis.keys()` is acceptable here as this is a low-frequency operation.
> If scale demands it, switch to SCAN-based iteration.

---

#### `change_password(db, user, body) -> None`

```
Input:  user (User), body (ChangePasswordRequest)
Errors: 400 if old_password is wrong
```

Logic:
1. `verify_password(body.old_password, user.hashed_password)` → if False → 400
2. `user.hashed_password = hash_password(body.new_password)`
3. `await db.flush()`
4. `await revoke_all_user_tokens(str(user.id))`

---

## 7. API Endpoints (`app/modules/auth/router.py`)

Base prefix: `/v1/auth`

---

### `POST /v1/auth/register`

**Purpose**: Create a new user account.

**Auth**: None (public)

**Request Body**:
```json
{
  "email": "jane@example.com",
  "password": "mypassword123",
  "display_name": "Jane"      // optional
}
```

**Success Response** `201 Created`:
```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "email": "jane@example.com",
  "display_name": "Jane"
}
```

**Error Responses**:
| Status | Condition |
|--------|-----------|
| `409 Conflict` | Email already registered |
| `422 Unprocessable Entity` | Invalid email format or missing fields |

**Handler Logic**:
```python
@router.post("/register", response_model=UserResponse, status_code=201)
async def register(body: RegisterRequest, db: AsyncSession = Depends(get_db)):
    user = await register_user(db, body)
    return user
```

---

### `POST /v1/auth/login`

**Purpose**: Authenticate user, return JWT token pair.

**Auth**: None (public)

**Request Body**:
```json
{
  "email": "jane@example.com",
  "password": "mypassword123"
}
```

**Success Response** `200 OK`:
```json
{
  "access_token": "<jwt>",
  "refresh_token": "<jwt>",
  "token_type": "bearer"
}
```

**Error Responses**:
| Status | Condition |
|--------|-----------|
| `401 Unauthorized` | Wrong email or password (intentionally vague for security) |

**Handler Logic**:
```python
@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, db: AsyncSession = Depends(get_db)):
    user = await authenticate_user(db, body.email, body.password)
    if user is None:
        raise HTTPException(status_code=401, detail="Incorrect email or password")
    access_token, refresh_token, _ = await issue_tokens(str(user.id))
    return TokenResponse(access_token=access_token, refresh_token=refresh_token)
```

---

### `POST /v1/auth/refresh`

**Purpose**: Exchange a valid refresh token for a new token pair (rotation).

**Auth**: None (token is in request body)

**Request Body**:
```json
{
  "refresh_token": "<jwt>"
}
```

**Success Response** `200 OK`:
```json
{
  "access_token": "<new_jwt>",
  "refresh_token": "<new_jwt>",
  "token_type": "bearer"
}
```

**Error Responses**:
| Status | Condition |
|--------|-----------|
| `401 Unauthorized` | Token invalid, expired, or already revoked |

**Handler Logic**:
```python
@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(body: RefreshRequest):
    try:
        access_token, refresh_token = await refresh_tokens(body.refresh_token)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")
    return TokenResponse(access_token=access_token, refresh_token=refresh_token)
```

---

### `POST /v1/auth/logout`

**Purpose**: Revoke the current refresh token.

**Auth**: Bearer access token required

**Request Body**:
```json
{
  "refresh_token": "<jwt>"
}
```

> The client must send the refresh token so the server can revoke it from Redis.
> The access token (short-lived) is left to expire naturally.

**Success Response** `204 No Content`

**Error Responses**:
| Status | Condition |
|--------|-----------|
| `401 Unauthorized` | Missing or invalid access token |

**Handler Logic**:
```python
@router.post("/logout", status_code=204)
async def logout(
    body: RefreshRequest,
    current_user: User = Depends(get_current_user),
):
    try:
        payload = decode_access_token(body.refresh_token)
        jti = payload.get("jti")
        if jti:
            await revoke_refresh_token(str(current_user.id), jti)
    except ValueError:
        pass  # Token already invalid, logout is still successful
```

---

### `POST /v1/auth/change-password`

**Purpose**: Change the authenticated user's password and revoke all sessions.

**Auth**: Bearer access token required

**Request Body**:
```json
{
  "old_password": "mypassword123",
  "new_password": "newpassword456"
}
```

**Success Response** `204 No Content`

**Error Responses**:
| Status | Condition |
|--------|-----------|
| `400 Bad Request` | `old_password` is incorrect |
| `401 Unauthorized` | Missing or invalid access token |
| `422 Unprocessable Entity` | `new_password` less than 8 characters |

**Handler Logic**:
```python
@router.post("/change-password", status_code=204)
async def change_password_endpoint(
    body: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await change_password(db, current_user, body)
```

---

## 8. Alembic Migration

Run after the code is in place:

```bash
# Generate migration (autogenerate from models)
alembic revision --autogenerate -m "create users table"

# Review the generated file in alembic/versions/
# Then apply:
alembic upgrade head
```

> [!IMPORTANT]
> Always review the auto-generated migration file before applying.
> Ensure it only contains the `users` table creation, not destructive changes.

---

## 9. Error Handling Summary

All errors follow FastAPI's default JSON format:
```json
{
  "detail": "Human-readable error message"
}
```

| Scenario | HTTP Status |
|----------|-------------|
| Email already registered | 409 |
| Wrong credentials | 401 |
| Invalid / expired token | 401 |
| Inactive user | 403 |
| Wrong old password | 400 |
| Validation error (Pydantic) | 422 |
