# pigagent/api/roast.py
"""Roast API endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from loguru import logger

from bootstrap.factory import create_pig_agent, get_game_modes

router = APIRouter(prefix="/roast", tags=["roast"])


class RoastStartRequest(BaseModel):
    user_id: str
    persona_id: int
    roast_id: str
    mode_id: str
    prompt: str
    headline: str = ""
    teaser: str = ""
    source: str = ""


async def _event_stream(pig_agent, persona_id: int, roast_id: str,
                         mode_id: str, prompt: str,
                         headline: str = "", teaser: str = "", source: str = ""):
    """SSE generator: yields text chunks from start_roast()."""
    try:
        async for text in pig_agent.start_roast(
            persona_id=persona_id,
            roast_id=roast_id,
            mode_id=mode_id,
            prompt=prompt,
            headline=headline, teaser=teaser,
            source=source,
        ):
            yield f"data: {{\"text\": {__import__('json').dumps(text)}}}\n\n"
        yield 'data: {"done": true}\n\n'
    except Exception as e:
        logger.error(f"[API] Roast stream failed: {e}")
        yield f'data: {{"error": {__import__("json").dumps(str(e))}}}\n\n'


@router.post("/start")
async def start_roast(req: RoastStartRequest):
    """Start a roast game and stream the opening reply as SSE.

    Two paths:
    - Agent in room → inject via LiveKit data channel → return settled_in_room
    - No agent → create temporary PigAgent → stream text via SSE
    """
    from roast.session_registry import registry

    game_modes = get_game_modes()

    # Validate game mode exists
    if req.mode_id not in game_modes:
        raise HTTPException(status_code=400, detail=f"Unknown game mode: {req.mode_id}")

    # Check if agent is already in the user's LiveKit room
    agent_active = await registry.has_active_agent(req.user_id)

    if agent_active:
        # Route through LiveKit room data channel — session's PigAgent handles it
        await registry.send_inject(req.user_id, {
            "type": "start_roast",
            "persona_id": req.persona_id,
            "roast_id": req.roast_id,
            "mode_id": req.mode_id,
            "prompt": req.prompt,
            "headline": req.headline, "teaser": req.teaser,
            "source": req.source,
        })
        # Return SSE with settled_in_room marker so the client knows
        async def _settled():
            yield 'data: {"settled_in_room": true, "done": true}\n\n'
        return StreamingResponse(
            _settled(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    # No agent — create temporary PigAgent and stream via SSE
    pig_agent = await create_pig_agent(req.user_id)

    return StreamingResponse(
        _event_stream(
            pig_agent,
            persona_id=req.persona_id,
            roast_id=req.roast_id,
            mode_id=req.mode_id,
            prompt=req.prompt,
            headline=req.headline, teaser=req.teaser,
            source=req.source,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


@router.post("/start-sync")
async def start_roast_sync(req: RoastStartRequest):
    """Start a roast synchronously — called by the API server's unified WS.

    Two paths:
    - Agent in room → inject via LiveKit data channel → return settled_in_room: true
    - No agent → generate opening text via LLM → return text for WS streaming
    """
    from roast.session_registry import registry

    game_modes = get_game_modes()

    if req.mode_id not in game_modes:
        raise HTTPException(status_code=400, detail=f"Unknown game mode: {req.mode_id}")

    # Check if agent is already in the user's LiveKit room
    agent_active = await registry.has_active_agent(req.user_id)

    if agent_active:
        # Route through LiveKit room data channel
        await registry.send_inject(req.user_id, {
            "type": "start_roast",
            "persona_id": req.persona_id,
            "roast_id": req.roast_id,
            "mode_id": req.mode_id,
            "prompt": req.prompt,
            "headline": req.headline, "teaser": req.teaser,
            "source": req.source,
        })
        return {
            "ok": True,
            "settled_in_room": True,
        }

    # No agent — generate opening text for WS streaming
    pig_agent = await create_pig_agent(req.user_id)
    try:
        full_text = ""
        async for text in pig_agent.start_roast(
            persona_id=req.persona_id,
            roast_id=req.roast_id,
            mode_id=req.mode_id,
            prompt=req.prompt,
            headline=req.headline, teaser=req.teaser,
            source=req.source,
        ):
            if isinstance(text, str):
                full_text += text

        return {
            "ok": True,
            "settled_in_room": False,
            "text": full_text.strip(),
        }
    except Exception as e:
        logger.error(f"[API] start_roast_sync failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
