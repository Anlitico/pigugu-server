"""In-process pub/sub event bus for roast session events.

Keyed by user_id (not roast_instance_id) because there is at most one
active roast per user at any time (RoastState enforces this).
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

from loguru import logger


class RoastEventBus:
    """Lightweight pub/sub for forwarding LiveKit events to WebSocket clients."""

    def __init__(self):
        self._subscribers: dict[str, list[asyncio.Queue]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def publish(self, user_id: str, event: dict[str, Any]) -> None:
        """Push an event to all subscribers for this user."""
        async with self._lock:
            queues = list(self._subscribers.get(user_id, []))
        for q in queues:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning(
                    f"[EventBus] Queue full for {user_id}, dropping event {event.get('type')}"
                )

    async def subscribe(self, user_id: str) -> asyncio.Queue:
        """Create a subscription queue for this user."""
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        async with self._lock:
            self._subscribers[user_id].append(q)
        logger.debug(
            f"[EventBus] Subscriber added for {user_id} "
            f"(total: {len(self._subscribers[user_id])})"
        )
        return q

    async def unsubscribe(self, user_id: str, queue: asyncio.Queue) -> None:
        """Remove a specific subscriber queue."""
        async with self._lock:
            if user_id in self._subscribers:
                self._subscribers[user_id] = [
                    q for q in self._subscribers[user_id] if q is not queue
                ]
                if not self._subscribers[user_id]:
                    del self._subscribers[user_id]
                logger.debug(f"[EventBus] Subscriber removed for {user_id}")


# Global singleton -- imported wherever publish/subscribe is needed
event_bus = RoastEventBus()
