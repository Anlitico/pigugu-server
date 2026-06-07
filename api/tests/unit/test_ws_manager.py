"""Unit tests for WebSocketManager — local dict + Redis Pub/Sub cross-pod delivery."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def ws_manager():
    """Fresh WebSocketManager for each test."""
    from modules.ws.manager import WebSocketManager
    return WebSocketManager()


@pytest.fixture
def mock_ws():
    """Mock WebSocket that accepts and sends."""
    ws = AsyncMock()
    ws.send_text = AsyncMock()
    return ws


# ── Helpers ─────────────────────────────────────────────────────

async def _async_idle():
    """Async generator that sleeps forever — simulates idle Redis listener."""
    while True:
        await asyncio.sleep(3600)
        yield  # pragma: no cover


async def _async_messages(messages: list[dict]):
    """Async generator yielding then sleeping forever."""
    for m in messages:
        yield m
    while True:
        await asyncio.sleep(3600)
        yield  # pragma: no cover


def _make_redis_mock(mock_pubsub):
    """Create a mock Redis that returns ``mock_pubsub`` synchronously."""
    mock_redis = MagicMock()  # NOT AsyncMock — pubsub() is a regular method
    mock_redis.pubsub.return_value = mock_pubsub
    return mock_redis


# ═══════════════════════════════════════════════════════════════
# connect / disconnect
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_connect_stores_and_accepts(ws_manager, mock_ws):
    mock_pubsub = AsyncMock()
    mock_pubsub.psubscribe = AsyncMock()
    mock_pubsub.listen.return_value = _async_idle()

    with patch("modules.ws.manager.from_url",
               return_value=_make_redis_mock(mock_pubsub)):
        await ws_manager.connect("dev1", mock_ws)

    mock_ws.accept.assert_called_once()
    assert "dev1" in ws_manager._connections
    assert ws_manager._connections["dev1"] is mock_ws


@pytest.mark.asyncio
async def test_connect_second_device_reuses_listener(ws_manager, mock_ws):
    """Second connect() should not create another Redis connection."""
    ws2 = AsyncMock()
    ws2.send_text = AsyncMock()

    mock_pubsub = AsyncMock()
    mock_pubsub.psubscribe = AsyncMock()
    mock_pubsub.listen.return_value = _async_idle()

    with patch("modules.ws.manager.from_url") as mock_from_url:
        mock_from_url.return_value = _make_redis_mock(mock_pubsub)
        await ws_manager.connect("dev1", mock_ws)
        await ws_manager.connect("dev2", ws2)

    # from_url called only once (listener reused)
    assert mock_from_url.call_count == 1
    assert "dev1" in ws_manager._connections
    assert "dev2" in ws_manager._connections


def test_disconnect_removes_entry(ws_manager, mock_ws):
    ws_manager._connections["dev1"] = mock_ws
    ws_manager.disconnect("dev1")
    assert "dev1" not in ws_manager._connections


def test_disconnect_unknown_device_no_error(ws_manager):
    ws_manager.disconnect("nonexistent")  # does not raise


# ═══════════════════════════════════════════════════════════════
# broadcast — fast path (local WS present)
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_broadcast_fast_path_sends_locally(ws_manager, mock_ws):
    ws_manager._connections["dev1"] = mock_ws

    with patch("modules.ws.manager.get_redis") as mock_get_redis:
        await ws_manager.broadcast("dev1", '{"event":"online"}')

    mock_ws.send_text.assert_called_once_with('{"event":"online"}')
    mock_get_redis.assert_not_called()


@pytest.mark.asyncio
async def test_broadcast_fast_path_failure_falls_through_to_redis(ws_manager, mock_ws):
    mock_ws.send_text.side_effect = RuntimeError("connection lost")
    ws_manager._connections["dev1"] = mock_ws

    mock_redis = AsyncMock()
    mock_redis.publish = AsyncMock()

    with patch("modules.ws.manager.get_redis", return_value=mock_redis):
        await ws_manager.broadcast("dev1", '{"event":"online"}')

    assert "dev1" not in ws_manager._connections
    mock_redis.publish.assert_called_once_with("ws:device:dev1", '{"event":"online"}')


# ═══════════════════════════════════════════════════════════════
# broadcast — cross-pod path (local WS absent)
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_broadcast_redis_path_publishes(ws_manager):
    mock_redis = AsyncMock()
    mock_redis.publish = AsyncMock()

    with patch("modules.ws.manager.get_redis", return_value=mock_redis):
        await ws_manager.broadcast("dev1", '{"event":"online"}')

    mock_redis.publish.assert_called_once_with("ws:device:dev1", '{"event":"online"}')


@pytest.mark.asyncio
async def test_broadcast_redis_failure_logs_warning_does_not_crash(ws_manager, caplog):
    mock_redis = AsyncMock()
    mock_redis.publish.side_effect = RuntimeError("redis down")

    with patch("modules.ws.manager.get_redis", return_value=mock_redis):
        await ws_manager.broadcast("dev1", '{"event":"online"}')  # does not raise

    assert "WS broadcast via Redis failed" in caplog.text


# ═══════════════════════════════════════════════════════════════
# _ensure_listener — double-checked locking
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_ensure_listener_creates_once(ws_manager):
    """Two concurrent _ensure_listener calls should create exactly one listener."""
    mock_pubsub = AsyncMock()
    mock_pubsub.psubscribe = AsyncMock()
    mock_pubsub.listen.return_value = _async_idle()

    with patch("modules.ws.manager.from_url") as mock_from_url:
        mock_from_url.return_value = _make_redis_mock(mock_pubsub)
        await asyncio.gather(
            ws_manager._ensure_listener(),
            ws_manager._ensure_listener(),
        )

    assert mock_from_url.call_count == 1
    mock_pubsub.psubscribe.assert_called_once()


# ═══════════════════════════════════════════════════════════════
# _listen — Redis Pub/Sub message forwarding
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_listener_forwards_message_to_local_ws(ws_manager, mock_ws):
    """When a pmessage arrives, listener should forward to local WS."""
    ws_manager._connections["dev1"] = mock_ws

    mock_pubsub = AsyncMock()
    mock_pubsub.listen = MagicMock(return_value=_async_messages([
        {"type": "pmessage", "channel": "ws:device:dev1",
         "pattern": "ws:device:*", "data": '{"event":"online"}'},
    ]))

    ws_manager._pubsub = mock_pubsub
    listener_task = asyncio.create_task(ws_manager._listen())

    await asyncio.sleep(0.1)
    listener_task.cancel()
    try:
        await listener_task
    except asyncio.CancelledError:
        pass

    mock_ws.send_text.assert_called_once_with('{"event":"online"}')


@pytest.mark.asyncio
async def test_listener_ignores_unknown_device(ws_manager, mock_ws):
    """Message for a device not in _connections should be ignored."""
    ws_manager._connections["dev2"] = mock_ws

    mock_pubsub = AsyncMock()
    mock_pubsub.listen = MagicMock(return_value=_async_messages([
        {"type": "pmessage", "channel": "ws:device:dev1",
         "pattern": "ws:device:*", "data": '{"event":"online"}'},
    ]))

    ws_manager._pubsub = mock_pubsub
    listener_task = asyncio.create_task(ws_manager._listen())

    await asyncio.sleep(0.1)
    listener_task.cancel()
    try:
        await listener_task
    except asyncio.CancelledError:
        pass

    mock_ws.send_text.assert_not_called()


@pytest.mark.asyncio
async def test_listener_removes_dead_ws(ws_manager, mock_ws):
    """When send_text fails, the dead WS should be removed."""
    mock_ws.send_text.side_effect = RuntimeError("pipe broken")
    ws_manager._connections["dev1"] = mock_ws

    mock_pubsub = AsyncMock()
    mock_pubsub.listen = MagicMock(return_value=_async_messages([
        {"type": "pmessage", "channel": "ws:device:dev1",
         "pattern": "ws:device:*", "data": '{"event":"online"}'},
    ]))

    ws_manager._pubsub = mock_pubsub
    listener_task = asyncio.create_task(ws_manager._listen())

    await asyncio.sleep(0.1)
    listener_task.cancel()
    try:
        await listener_task
    except asyncio.CancelledError:
        pass

    assert "dev1" not in ws_manager._connections


@pytest.mark.asyncio
async def test_listener_reconnects_on_error(ws_manager):
    """Listener should reconnect when Redis Pub/Sub connection drops."""
    old_pubsub = AsyncMock()
    # MagicMock so .listen() raises immediately (not returning a coroutine)
    # which makes async for fail correctly
    old_pubsub.listen = MagicMock(side_effect=RuntimeError("connection lost"))
    old_pubsub.punsubscribe = AsyncMock()

    old_redis = MagicMock()
    old_redis.aclose = AsyncMock()

    new_pubsub = AsyncMock()
    new_pubsub.psubscribe = AsyncMock()
    new_pubsub.punsubscribe = AsyncMock()
    new_pubsub.listen.return_value = _async_idle()

    ws_manager._pubsub = old_pubsub
    ws_manager._pubsub_redis = old_redis
    # Pre-set to avoid the None guard in _ensure_listener confusing the test
    ws_manager._listener_task = object()

    with patch("modules.ws.manager.from_url",
               return_value=_make_redis_mock(new_pubsub)):
        listener_task = asyncio.create_task(ws_manager._listen())

        # Give listener time to fail + reconnect (real 1s backoff)
        await asyncio.sleep(1.2)

        # After reconnect, the listener should be healthy
        assert ws_manager._pubsub is new_pubsub
        new_pubsub.psubscribe.assert_called_once_with("ws:device:*")

        listener_task.cancel()
        try:
            await listener_task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_listener_clears_task_on_permanent_failure(ws_manager):
    """When reconnect fails, _listener_task should be set to None."""
    old_pubsub = AsyncMock()
    old_pubsub.listen = MagicMock(side_effect=RuntimeError("connection lost"))
    old_pubsub.punsubscribe = AsyncMock()

    old_redis = MagicMock()
    old_redis.aclose = AsyncMock()

    ws_manager._pubsub = old_pubsub
    ws_manager._pubsub_redis = old_redis
    # Pre-set to avoid the None guard
    ws_manager._listener_task = object()

    with patch("modules.ws.manager.from_url",
               side_effect=RuntimeError("redis gone")):
        listener_task = asyncio.create_task(ws_manager._listen())

        # Give listener time to fail + attempt reconnect (real 1s backoff)
        await asyncio.sleep(1.2)
        # Task should have exited after reconnect failure
        assert listener_task.done()
        # On permanent failure, clears so next connect() can retry
        assert ws_manager._listener_task is None
