import asyncio
import logging
from fastapi import WebSocket
from redis.asyncio import from_url
from redis.asyncio.client import PubSub

from core.config import settings
from core.redis import get_redis

logger = logging.getLogger(__name__)

CHANNEL_PREFIX = "ws:device:"


class WebSocketManager:
    """Manages WebSocket connections per device_id, with cross-pod delivery via Redis Pub/Sub.

    Architecture:
      - Local dict ``_connections[device_id]`` for fast same-pod delivery.
      - Each pod subscribes to ``ws:device:*`` via Redis pattern Pub/Sub.
      - ``broadcast()`` tries the local connection first; if not found, publishes
        to Redis so the pod that owns the WS can deliver it.
    """

    def __init__(self) -> None:
        self._connections: dict[str, WebSocket] = {}
        self._pubsub: PubSub | None = None
        self._pubsub_redis = None
        self._listener_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    # ── Redis Pub/Sub listener (starts lazily on first WS connect) ──

    async def _ensure_listener(self) -> None:
        if self._listener_task is not None:
            return

        async with self._lock:
            # Double-check after acquiring lock
            if self._listener_task is not None:
                return

            self._pubsub_redis = from_url(settings.redis_url, decode_responses=True)
            self._pubsub = self._pubsub_redis.pubsub()
            if self._pubsub is None:
                logger.error("Redis pubsub() returned None — cannot start listener")
                return
            await self._pubsub.psubscribe(f"{CHANNEL_PREFIX}*")
            self._listener_task = asyncio.create_task(self._listen())
            logger.info("WS pubsub listener started (pattern=%s*)", CHANNEL_PREFIX)

    async def _listen(self) -> None:
        if self._pubsub is None:
            logger.error("_listen() called with no pubsub — aborting")
            return
        while True:
            try:
                async for message in self._pubsub.listen():
                    if message["type"] == "pmessage":
                        # channel = "ws:device:{device_id}", data = JSON payload
                        channel: str = message["channel"]
                        if channel.startswith(CHANNEL_PREFIX):
                            device_id = channel[len(CHANNEL_PREFIX):]
                            ws = self._connections.get(device_id)
                            if ws:
                                try:
                                    await ws.send_text(message["data"])
                                except Exception:
                                    logger.debug("WS send failed for %s, removing", device_id)
                                    self._connections.pop(device_id, None)
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.error("WS pubsub listener crashed, reconnecting in 1s: %s", e)
                await asyncio.sleep(1)
                try:
                    # Close old connection before creating a new one
                    await self._close_pubsub()
                    self._pubsub_redis = from_url(settings.redis_url, decode_responses=True)
                    self._pubsub = self._pubsub_redis.pubsub()
                    if self._pubsub is None:
                        logger.error("Redis pubsub() returned None — reconnect failed")
                        self._listener_task = None
                        return
                    await self._pubsub.psubscribe(f"{CHANNEL_PREFIX}*")
                except Exception as e2:
                    logger.error("Failed to reconnect pubsub: %s", e2)
                    self._listener_task = None  # allow next connect() to retry
                    return

    async def _close_pubsub(self) -> None:
        """Safely tear down the current Pub/Sub connection."""
        old = self._pubsub
        self._pubsub = None
        if old:
            try:
                await old.punsubscribe()
            except Exception:
                pass
        old_redis = self._pubsub_redis
        self._pubsub_redis = None
        if old_redis:
            try:
                await old_redis.aclose()
            except Exception:
                pass

    # ── Public API ────────────────────────────────────────────────

    async def connect(self, device_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections[device_id] = websocket
        await self._ensure_listener()

    def disconnect(self, device_id: str) -> None:
        self._connections.pop(device_id, None)

    async def handle_message(self, device_id: str, data: str) -> None:
        """Handle incoming WebSocket message. Currently a no-op — reserved for
        future client→server signalling."""
        pass

    async def broadcast(self, device_id: str, message: str) -> None:
        """Send ``message`` to the WebSocket registered for ``device_id``.

        Tries the local connection first (fast path — same pod). Falls back
        to Redis Pub/Sub so that whichever pod owns the WS can deliver it.
        """
        # Fast path: same-pod delivery
        ws = self._connections.get(device_id)
        if ws:
            try:
                await ws.send_text(message)
                return
            except Exception:
                # Local WS is dead; remove it and fall through to cross-pod
                self._connections.pop(device_id, None)

        # Cross-pod delivery via Redis Pub/Sub
        try:
            r = await get_redis()
            await r.publish(f"{CHANNEL_PREFIX}{device_id}", message)
        except Exception as e:
            logger.warning("WS broadcast via Redis failed for %s: %s", device_id, e)


ws_manager = WebSocketManager()
