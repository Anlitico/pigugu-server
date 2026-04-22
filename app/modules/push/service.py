import firebase_admin
from firebase_admin import credentials

from app.core.config import settings

_app: firebase_admin.App | None = None


def init_firebase() -> None:
    global _app
    cred = credentials.Certificate(settings.firebase_credentials_path)
    _app = firebase_admin.initialize_app(cred)


async def send_notification(
    token: str, title: str, body: str, data: dict | None = None
) -> str:
    """Returns FCM message_id."""
    ...


async def send_push(user_id: str, notification_type: str, payload: dict) -> None:
    """High-level helper: look up FCM tokens for user, call send_notification."""
    ...
