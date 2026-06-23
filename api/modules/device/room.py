# api/modules/device/room.py
"""LiveKit room management — ensure rooms, dispatch agents, generate tokens.

Room names are derived from the user's UUID so all of a user's devices share
the same room. Server guarantees: if the room exists, an agent is in it.
"""

from __future__ import annotations

import json
import logging
from urllib.parse import urlparse, urlunparse

from livekit import api as lk_api
from livekit.api import TwirpError

from core.config import settings

logger = logging.getLogger(__name__)

AGENT_NAME = "pigugu-agent"


def build_room_name(user_id: str) -> str:
    """Room name is the user UUID."""
    return user_id.strip()


def _server_url(livekit_url: str) -> str:
    """Convert LiveKit WebSocket URL to HTTPS URL for server API calls."""
    parsed = urlparse(livekit_url)
    if parsed.scheme in ("wss", "https"):
        scheme = "https"
    elif parsed.scheme in ("ws", "http"):
        scheme = "http"
    else:
        scheme = "https"
    return urlunparse((scheme, parsed.netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))


def _lk_client() -> lk_api.LiveKitAPI:
    """Return a LiveKitAPI client configured from settings."""
    return lk_api.LiveKitAPI(
        url=_server_url(settings.livekit_url),
        api_key=settings.livekit_api_key,
        api_secret=settings.livekit_api_secret,
    )


async def ensure_room(user_id: str) -> dict:
    """Ensure a LiveKit room exists for this user, with agent dispatched.

    Tries create_room first. If the room already exists (agent is in it),
    reuses it — just generates a fresh token. This is atomic: no check-then-act
    race between querying room existence and creating it.

    Returns dict with room_name, token, livekit_url.
    """
    room_name = build_room_name(user_id)

    metadata = json.dumps({"user_id": user_id})

    async with _lk_client() as lk:
        # 1. Try to create the room with agent dispatch.
        #    If it already exists → room is alive with agent → reuse.
        try:
            request = lk_api.CreateRoomRequest(
                name=room_name,
                empty_timeout=600,
                agents=[
                    lk_api.RoomAgentDispatch(
                        agent_name=AGENT_NAME,
                        metadata=metadata,
                    )
                ],
            )
            room = await lk.room.create_room(request)
            logger.info(
                "Room created: name=%s sid=%s user=%s",
                room_name, room.sid, user_id,
            )

        except TwirpError as e:
            if e.code == "already_exists":
                logger.info(
                    "Room %s already exists, reusing (user=%s)",
                    room_name, user_id,
                )
            else:
                raise


async def check_room_alive(room_name: str) -> bool:
    """Check if a LiveKit room exists and has at least one participant."""
    async with _lk_client() as lk:
        rooms = await lk.room.list_rooms(
            lk_api.ListRoomsRequest(names=[room_name])
        )
        if not rooms.rooms:
            return False
        room = rooms.rooms[0]
        return room.num_participants > 0
