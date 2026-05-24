"""Pending prompt bridge between roast state machine and context assembly.

tick() writes prompts here. Context assembly reads them via
consume() before building the message list.
"""

from __future__ import annotations

from loguru import logger

_KEY = "roast:{roast_id}:pending_prompt"
_TTL = 86400


async def consume(roast_id: str, redis) -> str | None:
    """Read and delete the pending prompt. Called by context assembly."""
    key = _KEY.format(roast_id=roast_id)
    try:
        prompt = await redis.get(key)
        if prompt:
            await redis.delete(key)
            return prompt if isinstance(prompt, str) else prompt.decode("utf-8")
    except Exception as e:
        logger.warning(f"[Pending] consume failed for {roast_id}: {e}")
    return None


async def write(roast_id: str, prompt: str, redis) -> None:
    """Write a pending prompt. Called by GameMode.tick()."""
    try:
        await redis.setex(_KEY.format(roast_id=roast_id), _TTL, prompt)
    except Exception as e:
        logger.error(f"[Pending] write failed for {roast_id}: {e}")
