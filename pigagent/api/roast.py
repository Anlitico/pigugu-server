# pigagent/api/roast.py
"""Roast API endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from loguru import logger

from bootstrap.factory import get_pig_agent

router = APIRouter(prefix="/roast", tags=["roast"])


class RoastStartRequest(BaseModel):
    user_id: str
    persona_id: int
    roast_id: str
    mode_id: str
    prompt: str
    headline: str = ""
    source: str = ""


async def _event_stream(user_id: str, persona_id: int, roast_id: str,
                         mode_id: str, prompt: str,
                         headline: str = "", source: str = ""):
    """SSE generator: yields text chunks from start_roast()."""
    pig_agent = get_pig_agent()
    try:
        async for text in pig_agent.start_roast(
            user_id=user_id,
            persona_id=persona_id,
            roast_id=roast_id,
            mode_id=mode_id,
            prompt=prompt,
            headline=headline,
            source=source,
        ):
            yield f"data: {{\"text\": {__import__('json').dumps(text)}}}\n\n"
        yield 'data: {"done": true}\n\n'
    except Exception as e:
        logger.error(f"[API] Roast stream failed: {e}")
        yield f'data: {{"error": {__import__("json").dumps(str(e))}}}\n\n'


@router.post("/start")
async def start_roast(req: RoastStartRequest):
    """Start a roast game and stream the opening reply as SSE."""
    pig_agent = get_pig_agent()

    # Validate game mode exists
    if req.mode_id not in pig_agent._game_modes:
        raise HTTPException(status_code=400, detail=f"Unknown game mode: {req.mode_id}")

    return StreamingResponse(
        _event_stream(
            user_id=req.user_id,
            persona_id=req.persona_id,
            roast_id=req.roast_id,
            mode_id=req.mode_id,
            prompt=req.prompt,
            headline=req.headline,
            source=req.source,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )
