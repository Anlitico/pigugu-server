import logging
import uuid
from pathlib import Path

import firebase_admin
from firebase_admin import credentials, messaging
from sqlalchemy import select

from core.database import AsyncSessionLocal
from models.fcm_token import FCMToken

logger = logging.getLogger(__name__)

# Lazy-init Firebase app (singleton)
_app: firebase_admin.App | None = None


def _get_app() -> firebase_admin.App:
    global _app
    if _app is None:
        # Service account key from project root config/
        key_path = Path(__file__).resolve().parent.parent.parent / "config" / "firebase-service-account.json"
        cred = credentials.Certificate(str(key_path))
        _app = firebase_admin.initialize_app(cred)
        logger.info("Firebase Admin initialized from %s", key_path)
    return _app


async def get_user_tokens(user_id: uuid.UUID) -> list[str]:
    """Return all FCM tokens for a user."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(FCMToken.token).where(FCMToken.user_id == user_id)
        )
        return [row[0] for row in result.all()]


async def register_token(user_id: uuid.UUID, token: str, platform: str | None = None) -> None:
    """Upsert an FCM token for a user."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(FCMToken).where(FCMToken.token == token)
        )
        existing = result.scalar_one_or_none()
        if existing:
            if existing.user_id != user_id:
                existing.user_id = user_id
            if platform:
                existing.platform = platform
            await db.commit()
        else:
            db.add(FCMToken(user_id=user_id, token=token, platform=platform))
            await db.commit()


async def send_push(user_id: uuid.UUID, title: str, body: str,
                    data: dict | None = None) -> int:
    """Send push notification to all of a user's devices.

    Returns the number of messages sent.
    """
    tokens = await get_user_tokens(user_id)
    if not tokens:
        logger.debug("No FCM tokens for user %s", user_id)
        return 0

    _get_app()

    android_config = messaging.AndroidConfig(
        priority="high",
        notification=messaging.AndroidNotification(
            channel_id="pigugu_default",
            priority="high",
        ),
    )

    apns_config = messaging.APNSConfig(
        payload=messaging.APNSPayload(
            aps=messaging.Aps(alert=messaging.ApsAlert(title=title, body=body),
                              sound="default"),
        ),
    )

    msg = messaging.MulticastMessage(
        tokens=tokens,
        notification=messaging.Notification(title=title, body=body),
        data={k: str(v) for k, v in (data or {}).items()} if data else None,
        android=android_config,
        apns=apns_config,
    )

    response = messaging.send_each_for_multicast(msg)
    success_count = response.success_count
    logger.info("FCM push: %d/%d delivered (user=%s, title=%s)",
                success_count, len(tokens), user_id, title)

    # Clean up invalid tokens
    for i, resp in enumerate(response.responses):
        if not resp.success and resp.exception:
            code = resp.exception.code
            if code in ("UNREGISTERED", "INVALID_ARGUMENT"):
                logger.info("Removing stale FCM token: %s", tokens[i][:20])
                await _delete_token(tokens[i])

    return success_count


async def _delete_token(token: str) -> None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(FCMToken).where(FCMToken.token == token)
        )
        existing = result.scalar_one_or_none()
        if existing:
            await db.delete(existing)
            await db.commit()
