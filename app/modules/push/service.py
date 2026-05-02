import firebase_admin
from firebase_admin import credentials

from app.core.config import settings

_app: firebase_admin.App | None = None


def init_firebase() -> None:
    global _app
    import os
    if not os.path.exists(settings.firebase_credentials_path):
        print(f"Warning: Firebase credentials file not found at {settings.firebase_credentials_path}. Push notifications will be disabled.")
        return
    try:
        cred = credentials.Certificate(settings.firebase_credentials_path)
        _app = firebase_admin.initialize_app(cred)
        print("Firebase initialized successfully.")
    except Exception as e:
        print(f"Error initializing Firebase: {e}. Push notifications will be disabled.")


async def send_notification(
    token: str, title: str, body: str, data: dict | None = None
) -> str:
    """Returns FCM message_id."""
    ...


async def send_push(user_id: str, notification_type: str, payload: dict) -> None:
    """High-level helper: look up FCM tokens for user, call send_notification."""
    ...
