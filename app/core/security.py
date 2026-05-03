import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
from jose import JWTError, jwt

from app.core.config import settings


def hash_password(plain_password: str) -> str:
    """Hash a password using bcrypt."""
    # bcrypt requires bytes as input
    pwd_bytes = plain_password.encode("utf-8")
    # Generate a salt and hash the password
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(pwd_bytes, salt)
    # Return as string for database storage
    return hashed.decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plain password against its hash."""
    try:
        password_bytes = plain_password.encode("utf-8")
        hashed_bytes = hashed_password.encode("utf-8")
        return bcrypt.checkpw(password_bytes, hashed_bytes)
    except Exception:
        return False


def create_access_token(subject: str, extra: dict | None = None) -> str:
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=settings.jwt_access_token_expire_minutes
    )
    payload = {"sub": subject, "exp": expire, **(extra or {})}
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def create_refresh_token(subject: str) -> tuple[str, str]:
    jti = str(uuid.uuid4())
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=settings.jwt_refresh_token_expire_minutes
    )
    payload = {
        "sub": subject,
        "exp": expire,
        "jti": jti,
        "type": "refresh",
    }
    token = jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    return token, jti


def decode_access_token(token: str) -> dict:
    try:
        return jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    except JWTError as exc:
        raise ValueError("Invalid token") from exc


def create_mqtt_token(hw_id: str) -> tuple[str, str, datetime]:
    """Create a short-lived MQTT auth token for device connectivity.

    Returns (token, jti, expires_at).
    """
    jti = str(uuid.uuid4())
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.mqtt_jwt_expire_minutes)
    payload = {
        "sub": hw_id,
        "hw_id": hw_id,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
        "iss": "pigugu-server",
        "token_type": "mqtt",
        "jti": jti,
    }
    token = jwt.encode(payload, settings.mqtt_jwt_secret_key, algorithm=settings.mqtt_jwt_algorithm)
    return token, jti, expire


def decode_mqtt_token(token: str, verify_exp: bool = True) -> dict:
    """Decode and validate an MQTT auth token.

    Raises ValueError if the token is invalid, not an MQTT token, or expired.
    Pass verify_exp=False to allow decoding an expired token for the refresh grace period.
    """
    options = {"verify_exp": verify_exp}
    try:
        payload = jwt.decode(
            token,
            settings.mqtt_jwt_secret_key,
            algorithms=[settings.mqtt_jwt_algorithm],
            options=options,
        )
        if payload.get("token_type") != "mqtt":
            raise ValueError("Token is not an MQTT token")
        return payload
    except JWTError as exc:
        raise ValueError("Invalid MQTT token") from exc
