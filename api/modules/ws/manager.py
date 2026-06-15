import asyncio
import logging
from fastapi import WebSocket
from redis.asyncio import from_url
from redis.asyncio.client import PubSub

from core.config import settings
from core.redis import get_redis

logger = logging.getLogger(__name__)

WS_CHANNEL_PREFIX = "ws:app:"
WS_USER_CHANNEL_PREFIX = "ws:user:"
CONNECTION_KEY_SEP = ":"  # user_id:app_device_id


class WebSocketManager:
    """Manages WebSocket connections keyed by ``user_id:app_device_id``.

    Architecture:
      - ``_connections[key]`` — local dict of WebSocket objects (process-bound).
      - NO in-process user index — ``broadcast_to_user`` always goes through
        Redis Pub/Sub. Every pod's listener iterates its own ``_connections``
        to find matching keys and deliver the message.
      - Redis Pub/Sub patterns: ``ws:app:*`` (point-to-point),
        ``ws:user:*`` (fan-out to all of a user's devices).
    """

    def __init__(self) -> None:
        self._connections: dict[str, WebSocket] = {}
        self._pubsub: PubSub | None = None
        self._pubsub_redis = None
        self._listener_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    @staticmethod
    def make_key(user_id: str, app_device_id: str) -> str:
        return f"{user_id}{CONNECTION_KEY_SEP}{app_device_id}"

    # ── Redis Pub/Sub listener ──────────────────────────────────────

    async def _ensure_listener(self) -> None:
        if self._listener_task is not None:
            return

        async with self._lock:
            if self._listener_task is not None:
                return

            self._pubsub_redis = from_url(settings.redis_url, decode_responses=True)
            self._pubsub = self._pubsub_redis.pubsub()
            if self._pubsub is None:
                logger.error("Redis pubsub() returned None — cannot start listener")
                return
            await self._pubsub.psubscribe(
                f"{WS_CHANNEL_PREFIX}*",
                f"{WS_USER_CHANNEL_PREFIX}*",
            )
            self._listener_task = asyncio.create_task(self._listen())
            logger.info(
                "WS pubsub listener started (patterns=%s*, %s*)",
                WS_CHANNEL_PREFIX, WS_USER_CHANNEL_PREFIX,
            )

    async def _listen(self) -> None:
        if self._pubsub is None:
            return
        while True:
            try:
                async for message in self._pubsub.listen():
                    if message["type"] == "pmessage":
                        channel: str = message["channel"]
                        data = message["data"]

                        if channel.startswith(WS_USER_CHANNEL_PREFIX):
                            user_id = channel[len(WS_USER_CHANNEL_PREFIX):]
                            prefix = f"{user_id}{CONNECTION_KEY_SEP}"
                            for key, ws in list(self._connections.items()):
                                if key.startswith(prefix):
                                    try:
                                        await ws.send_text(data)
                                    except Exception:
                                        logger.debug("WS send failed for %s, removing", key)
                                        self._connections.pop(key, None)

                        elif channel.startswith(WS_CHANNEL_PREFIX):
                            key = channel[len(WS_CHANNEL_PREFIX):]
                            ws = self._connections.get(key)
                            if ws:
                                try:
                                    await ws.send_text(data)
                                except Exception:
                                    logger.debug("WS send failed for %s, removing", key)
                                    self._connections.pop(key, None)
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.error("WS pubsub listener crashed, reconnecting in 1s: %s", e)
                await asyncio.sleep(1)
                try:
                    await self._close_pubsub()
                    self._pubsub_redis = from_url(settings.redis_url, decode_responses=True)
                    self._pubsub = self._pubsub_redis.pubsub()
                    if self._pubsub is None:
                        self._listener_task = None
                        return
                    await self._pubsub.psubscribe(
                        f"{WS_CHANNEL_PREFIX}*",
                        f"{WS_USER_CHANNEL_PREFIX}*",
                    )
                except Exception as e2:
                    logger.error("Failed to reconnect pubsub: %s", e2)
                    self._listener_task = None
                    return

    async def _close_pubsub(self) -> None:
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

    # ── Public API ──────────────────────────────────────────────────

    async def connect(
        self, user_id: str, app_device_id: str, websocket: WebSocket,
    ) -> None:
        await websocket.accept()
        key = self.make_key(user_id, app_device_id)
        self._connections[key] = websocket
        await self._ensure_listener()
        logger.info("WS connected: key=%s", key)

    def disconnect(self, user_id: str, app_device_id: str) -> None:
        key = self.make_key(user_id, app_device_id)
        self._connections.pop(key, None)
        logger.info("WS disconnected: key=%s", key)

    async def broadcast(self, key: str, message: str) -> None:
        """Send ``message`` to a specific WS connection.

        Tries local first. Falls back to Redis Pub/Sub for cross-pod delivery.
        """
        ws = self._connections.get(key)
        if ws:
            try:
                await ws.send_text(message)
                return
            except Exception:
                self._connections.pop(key, None)

        try:
            r = await get_redis()
            await r.publish(f"{WS_CHANNEL_PREFIX}{key}", message)
        except Exception as e:
            logger.warning("WS broadcast failed for %s: %s", key, e)

    async def broadcast_to_user(self, user_id: str, message: str) -> None:
        """Send ``message`` to ALL WebSocket connections for a user.

        Always goes through Redis Pub/Sub. Every pod's listener forwards
        the message to its own matching connections.
        """
        try:
            r = await get_redis()
            await r.publish(f"{WS_USER_CHANNEL_PREFIX}{user_id}", message)
        except Exception as e:
            logger.warning("broadcast_to_user Redis failed for %s: %s", user_id, e)

    async def handle_message(self, key: str, data: str) -> None:
        """Handle incoming WebSocket message (reserved for future signalling)."""
        pass


ws_manager = WebSocketManager()
