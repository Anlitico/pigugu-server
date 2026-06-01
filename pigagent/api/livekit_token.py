"""LiveKit token endpoint for app-based audio integration.

Issues a join token so the Flutter app can connect to the same LiveKit
room as the agent, enabling phone-side audio I/O during roast sessions.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, Query
from livekit import api
from loguru import logger

from config import get_config

router = APIRouter(prefix="/livekit", tags=["livekit"])


@router.get("/token")
async def get_livekit_token(
    user_id: str = Query(...),
    room_name: str = Query(default="roast-room"),
):
    """Issue a LiveKit access token for the app to join a room.

    The same room_name is used by the hardware/agent worker.
    App connects as a publisher (mic) and subscriber (agent audio).
    """
    api_key = os.getenv("LIVEKIT_API_KEY")
    api_secret = os.getenv("LIVEKIT_API_SECRET")

    if not api_key or not api_secret:
        raise HTTPException(
            status_code=500,
            detail="LiveKit API credentials not configured on server",
        )

    config = get_config()

    try:
        token = (
            api.AccessToken(api_key=api_key, api_secret=api_secret)
            .with_identity(f"app-{user_id}")
            .with_name(f"App User {user_id}")
            .with_grants(
                api.VideoGrants(
                    room_join=True,
                    room=room_name,
                    can_publish=True,
                    can_subscribe=True,
                )
            )
            .to_jwt()
        )
    except Exception as e:
        logger.error(f"[LiveKit] Token generation failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to generate token")

    return {
        "token": token,
        "url": config.LIVEKIT_URL,
    }
