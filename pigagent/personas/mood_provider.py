# pigagent/context/mood_provider.py
"""
MoodProvider — reads/writes Pigugu's dynamic mood state.

Redis-backed with local in-memory fallback.
Keys: pigugu:mood:{session_id} → JSON MoodState
"""

from typing import Optional

from loguru import logger

from model import MoodState


class MoodProvider:
    """Manages Pigugu's emotional state for one session.

    Mood is updated per-turn based on user engagement and drifts
    naturally over time.
    """

    def __init__(self, redis_client=None):
        self._redis = redis_client
        self._local: dict[str, MoodState] = {}

    async def get(self, session_id: str) -> MoodState:
        """Get current mood for a session."""
        if self._redis:
            try:
                import json
                raw = await self._redis.get(f"pigugu:mood:{session_id}")
                if raw:
                    data = json.loads(raw)
                    return MoodState(**data)
            except Exception as e:
                logger.warning(f"Redis mood read failed: {e}")

        return self._local.get(session_id, MoodState())

    async def update(
        self,
        session_id: str,
        user_message: str = "",
        agent_response: str = "",
    ) -> MoodState:
        """Update mood based on the latest exchange and return new state."""
        current = await self.get(session_id)

        # Heuristic mood updates based on engagement signals
        msg_len = len(user_message)

        if msg_len > 80:
            current.excitement = min(1.0, current.excitement + 0.05)
            current.sarcasm = min(1.0, current.sarcasm + 0.03)
        elif msg_len < 10:
            current.excitement = max(0.0, current.excitement - 0.03)

        if any(word in user_message.lower() for word in
               ["angry", "outrageous", "ridiculous", "disaster", "terrible"]):
            current.anger = min(1.0, current.anger + 0.1)
            current.label = "burning"

        # Persist
        if self._redis:
            try:
                import json
                await self._redis.set(
                    f"pigugu:mood:{session_id}",
                    json.dumps({
                        "excitement": current.excitement,
                        "sarcasm": current.sarcasm,
                        "anger": current.anger,
                        "label": current.label,
                    }),
                )
            except Exception as e:
                logger.warning(f"Redis mood write failed: {e}")

        self._local[session_id] = current
        return current

    async def set_label(self, session_id: str, label: str) -> None:
        """Override mood label (e.g., 'chaos' for breaking news)."""
        current = await self.get(session_id)
        current.label = label
        self._local[session_id] = current
