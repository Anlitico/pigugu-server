"""Session Registry — check LiveKit room presence for a user.

Uses LiveKit's server API to check whether a pigagent worker
(AGENT participant) is active in the user's room, and to inject
commands into that room via the data channel.

Room naming convention: room_name = user_id (string).
App, hardware, and agent all join the same room.

Cross-process safe: queries the LiveKit server directly,
works across multiple pigagent pods.
"""

from __future__ import annotations

import json
import os

import aiohttp
from livekit.api.room_service import RoomService
from livekit.protocol.room import ListParticipantsRequest, SendDataRequest
from loguru import logger

# ParticipantInfo.kind values (LiveKit protobuf enum)
_KIND_AGENT = 4
# ParticipantInfo.state values
_STATE_ACTIVE = 2

# Data channel topic for roast-injection commands
TOPIC_ROAST_INJECT = "roast_inject"

# SendDataRequest.kind inline enum: 0 = RELIABLE, 1 = LOSSY.
# The Kind type is not importable (protobuf-upb limitation) — suppress pyright.
_DATA_KIND_RELIABLE: int = 0


class SessionRegistry:
    """LiveKit-based presence check + command injection.

    No registration needed — LiveKit tracks participants natively.
    Query the room to see if an AGENT is active, then send data
    into the room to trigger agent actions (e.g. start_roast).
    """

    def __init__(self) -> None:
        self._service: RoomService | None = None

    async def _get_service(self) -> RoomService:
        """Lazily initialise the RoomService client with LiveKit credentials."""
        if self._service is None:
            from agent_config import get_config

            cfg = get_config()
            api_key = os.getenv("LIVEKIT_API_KEY", "")
            api_secret = os.getenv("LIVEKIT_API_SECRET", "")

            if not api_key or not api_secret:
                logger.warning(
                    "[SessionRegistry] LIVEKIT_API_KEY/SECRET not set — "
                    "presence check will fail"
                )

            session = aiohttp.ClientSession()
            self._service = RoomService(
                session=session,
                url=cfg.LIVEKIT_URL,
                api_key=api_key,
                api_secret=api_secret,
            )
        return self._service

    async def has_active_agent(self, user_id: str) -> bool:
        """Check if the user's room has an ACTIVE agent participant."""
        room_name = str(user_id)

        service = await self._get_service()
        try:
            resp = await service.list_participants(
                ListParticipantsRequest(room=room_name)
            )
        except Exception as exc:
            logger.warning(
                f"[SessionRegistry] list_participants failed for "
                f"room={room_name}: {exc}"
            )
            return False

        for p in resp.participants:
            if p.kind == _KIND_AGENT and p.state == _STATE_ACTIVE:
                logger.info(
                    f"[SessionRegistry] Agent active in room {room_name} "
                    f"(identity={p.identity})"
                )
                return True

        logger.debug(
            f"[SessionRegistry] No active agent in room {room_name}"
        )
        return False

    async def send_inject(
        self,
        user_id: str,
        payload: dict,
    ) -> None:
        """Send a roast-inject command into the user's LiveKit room.

        The agent (session.py) listens on topic="roast_inject" and
        handles the command by running the full pig_agent pipeline.
        """
        room_name = str(user_id)
        service = await self._get_service()

        data = json.dumps(payload).encode("utf-8")
        await service.send_data(
            SendDataRequest(
                room=room_name,
                data=data,
                kind=_DATA_KIND_RELIABLE,  # pyright: ignore[reportArgumentType] — protobuf inline enum
                topic=TOPIC_ROAST_INJECT,
            )
        )
        logger.info(
            f"[SessionRegistry] Inject sent to room={room_name}: "
            f"type={payload.get('type')}"
        )


# Global singleton
registry = SessionRegistry()
