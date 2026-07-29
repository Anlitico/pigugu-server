"""Proxy endpoints that forward authenticated requests to pigugu-agent.

The agent is an internal service (ClusterIP, no public exposure).
These endpoints validate user auth via JWT, then forward to the agent
with a trusted internal identifier.
"""

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query

from core.deps import get_current_user
from models.user import User

AGENT_BASE = "http://pigugu-agent:8080"

logger = logging.getLogger(__name__)

router = APIRouter(tags=["agent"])


# ── HTTP proxy: /livekit/token ────────────────────────────────────────────────


@router.get("/livekit/token")
async def livekit_token_proxy(
    room_name: str = Query(default="roast-room"),
    current_user: User = Depends(get_current_user),
):
    """Proxy LiveKit token requests to the agent with authenticated user_id."""
    user_id = str(current_user.id)

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(
                f"{AGENT_BASE}/livekit/token",
                params={"user_id": user_id, "room_name": room_name},
                timeout=10.0,
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            logger.error(f"[agent-proxy] livekit/token failed: {e}")
            raise HTTPException(status_code=502, detail="Agent unavailable")


