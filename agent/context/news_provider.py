# agent/context/news_provider.py
"""
NewsProvider — fetches news context for the current conversation.

Pre-loaded by FastAPI before agent session starts. Accessed via
job metadata or Redis key lookup.
"""

from typing import Optional

from loguru import logger

from models import NewsContext


class NewsProvider:
    """Provides news context for agent sessions.

    News is assigned by the content layer (FastAPI) and passed via
    LiveKit job metadata or stored in Redis under pigugu:news:{news_id}.
    """

    def __init__(self, redis_client=None):
        self._redis = redis_client

    async def get(self, news_id: str) -> Optional[NewsContext]:
        """Fetch news context by ID."""
        if not news_id:
            return None

        if self._redis:
            try:
                import json
                raw = await self._redis.get(f"pigugu:news:{news_id}")
                if raw:
                    data = json.loads(raw)
                    return NewsContext(**data)
            except Exception as e:
                logger.warning(f"Redis news read failed: {e}")

        return None

    @staticmethod
    def from_metadata(metadata: dict) -> NewsContext:
        """Build NewsContext from LiveKit job metadata."""
        return NewsContext(
            news_id=metadata.get("news_id", ""),
            title=metadata.get("news_title", ""),
            summary=metadata.get("news_summary", ""),
            source=metadata.get("news_source", ""),
            domain=metadata.get("news_domain", ""),
            mode=metadata.get("mode", "roast"),
            persona=metadata.get("persona", "trump"),
        )
