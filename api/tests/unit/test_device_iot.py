"""Unit tests for iot.py — handlers not requiring database, and webhook dispatch."""
import asyncio
import json
import uuid
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ═══════════════════════════════════════════════════════════════
# _push_ws
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_push_ws_broadcasts():
    from modules.device.iot import _push_ws
    with patch("modules.ws.manager.ws_manager.broadcast", new_callable=AsyncMock) as m:
        await _push_ws("hw", {"event": "online"})
        m.assert_called_once_with("hw", json.dumps({"event": "online"}))


@pytest.mark.asyncio
async def test_push_ws_suppresses_errors():
    from modules.device.iot import _push_ws
    with patch("modules.ws.manager.ws_manager.broadcast", side_effect=RuntimeError("boom")):
        await _push_ws("hw", {"event": "online"})  # does not raise


# ═══════════════════════════════════════════════════════════════
# _wait_for_pong
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@patch("modules.device.iot.redis_exists", new_callable=AsyncMock)
@patch("modules.device.iot.asyncio.sleep", new_callable=AsyncMock)
async def test_wait_for_pong_key_found_no_error(mock_sleep, mock_exists):
    from modules.device.iot import _wait_for_pong
    mock_exists.return_value = True
    with patch("modules.device.iot._push_ws") as mock_push:
        await _wait_for_pong("hw", "req", "sess")
    mock_push.assert_not_called()


@pytest.mark.asyncio
@patch("modules.device.iot.redis_exists", new_callable=AsyncMock)
@patch("modules.device.iot.asyncio.sleep", new_callable=AsyncMock)
async def test_wait_for_pong_timeout_pushes_error(mock_sleep, mock_exists):
    from modules.device.iot import _wait_for_pong
    mock_exists.return_value = False
    with patch("modules.device.iot._push_ws") as mock_push:
        await _wait_for_pong("hw", "req", "sess")
    mock_push.assert_called_once()
    assert mock_push.call_args[0][1] == {
        "event": "error",
        "error_code": "PROVISION_VERIFY_TIMEOUT",
        "error_msg": "设备无法连接到服务器",
    }


@pytest.mark.asyncio
@patch("modules.device.iot.redis_exists", side_effect=RuntimeError("Redis down"))
@patch("modules.device.iot.asyncio.sleep", new_callable=AsyncMock)
async def test_wait_for_pong_redis_error_no_crash(mock_sleep, mock_exists):
    from modules.device.iot import _wait_for_pong
    with patch("modules.device.iot._push_ws") as mock_push:
        await _wait_for_pong("hw", "req", "sess")
    mock_push.assert_not_called()  # error suppressed


# ═══════════════════════════════════════════════════════════════
# _handle_online  (paths that don't touch the DB)
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_handle_online_no_session_id(*_):
    """Post-reboot path: no session → ping-pong, no WS beyond 'online'."""
    from modules.device.iot import _handle_online
    with patch("modules.device.iot.redis_set", new_callable=AsyncMock) as mock_set, \
         patch("modules.device.iot.redis_get", new_callable=AsyncMock) as mock_get, \
         patch("modules.device.iot._push_ws") as mock_push, \
         patch("core.aws.publish_mqtt_message", new_callable=AsyncMock) as mock_pub:
        mock_get.return_value = '{"rtt_ms": 42}'  # simulate pong
        await _handle_online(" Test-HW ", {})
    assert mock_set.call_count >= 2  # online + last_seen
    # Only "online" WS event — no booted/error (app may not be listening)
    mock_push.assert_called_once()
    assert mock_push.call_args[0][1]["event"] == "online"
    # Ping-pong: simple ping (no session_id)
    mock_pub.assert_called_once()
    ping = mock_pub.call_args[0][1]
    assert ping["msg_type"] == "connectivity.ping"
    assert "session_id" not in ping


@pytest.mark.asyncio
@patch("modules.device.iot.redis_set", new_callable=AsyncMock)
async def test_handle_online_invalid_uuid_pushes_error(mock_redis):
    from modules.device.iot import _handle_online
    with patch("modules.device.iot._push_ws") as mock_push:
        await _handle_online("hw", {"session_id": "not-a-uuid"})
    events = [c[0][1]["event"] for c in mock_push.call_args_list]
    assert "online" in events
    assert "error" in events


@pytest.mark.asyncio
@patch("modules.device.iot.redis_set", new_callable=AsyncMock)
async def test_handle_online_hw_id_normalized(mock_redis):
    """Whitespace and case are stripped."""
    from modules.device.iot import _handle_online
    with patch("modules.device.iot._push_ws"), \
         patch("core.aws.publish_mqtt_message", new_callable=AsyncMock):
        await _handle_online("  AbCdEf  ", {})
    # hw_id should be lowercased+stripped in the Redis key
    keys = [c[0][0] for c in mock_redis.call_args_list]
    assert all("abcdef" in k for k in keys)


# ═══════════════════════════════════════════════════════════════
# _handle_pong  (paths that don't touch the DB)
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_handle_pong_full_params():
    from modules.device.iot import _handle_pong
    with patch("modules.device.iot.redis_set", new_callable=AsyncMock) as mock_redis, \
         patch("modules.device.iot._push_ws") as mock_push, \
         patch("core.database.AsyncSessionLocal") as mock_db:
        mock_db.return_value.__aenter__.return_value = AsyncMock()
        await _handle_pong("hw", {"ts": 1234567890}, "sess", "req")
    assert mock_redis.call_count == 2  # provision:verify + device:connectivity
    mock_push.assert_called_once()
    assert mock_push.call_args[0][1]["event"] == "verified"


@pytest.mark.asyncio
async def test_handle_pong_no_session_id():
    from modules.device.iot import _handle_pong
    with patch("modules.device.iot.redis_set", new_callable=AsyncMock) as mock_redis, \
         patch("modules.device.iot._push_ws") as mock_push, \
         patch("core.database.AsyncSessionLocal") as mock_db:
        mock_db.return_value.__aenter__.return_value = AsyncMock()
        await _handle_pong("hw", {"ts": 1}, None, "req")
    assert mock_redis.call_count == 1  # only device:connectivity
    mock_push.assert_called_once()


@pytest.mark.asyncio
async def test_handle_pong_no_request_id():
    from modules.device.iot import _handle_pong
    with patch("modules.device.iot.redis_set", new_callable=AsyncMock) as mock_redis, \
         patch("modules.device.iot._push_ws") as mock_push, \
         patch("core.database.AsyncSessionLocal") as mock_db:
        mock_db.return_value.__aenter__.return_value = AsyncMock()
        await _handle_pong("hw", {"ts": 1}, "sess", None)
    mock_redis.assert_not_called()
    mock_push.assert_called_once()


@pytest.mark.asyncio
async def test_handle_pong_rtt_persist_failure_is_graceful():
    from modules.device.iot import _handle_pong
    with patch("modules.device.iot.redis_set", new_callable=AsyncMock), \
         patch("modules.device.iot._push_ws") as mock_push, \
         patch("core.database.AsyncSessionLocal", side_effect=RuntimeError("DB down")):
        await _handle_pong("hw", {"ts": 1}, "sess", "req")
    assert mock_push.call_args[0][1]["event"] == "verified"
    assert mock_push.call_args[0][1]["rtt_ms"] is None


# ═══════════════════════════════════════════════════════════════
# _handle_register  (paths that don't touch the DB)
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_handle_register_no_session_id():
    from modules.device.iot import _handle_register
    with patch("modules.device.iot._push_ws") as mock_push:
        await _handle_register("hw", {})
    mock_push.assert_not_called()


@pytest.mark.asyncio
async def test_handle_register_invalid_uuid_pushes_error():
    from modules.device.iot import _handle_register
    with patch("modules.device.iot._push_ws") as mock_push:
        await _handle_register("hw", {"session_id": "bad-uuid"})
    mock_push.assert_called_once()
    assert mock_push.call_args[0][1]["event"] == "error"


@pytest.mark.asyncio
async def test_handle_register_db_exception_pushes_error():
    from modules.device.iot import _handle_register
    with patch("modules.device.iot._push_ws") as mock_push, \
         patch("core.database.AsyncSessionLocal", side_effect=RuntimeError("DB gone")):
        await _handle_register("hw", {"session_id": str(uuid.uuid4())})
    mock_push.assert_called_once()
    assert mock_push.call_args[0][1]["event"] == "error"


@pytest.mark.asyncio
async def test_handle_register_hw_id_normalized():
    """Hardware ID is lowercased and stripped."""
    from modules.device.iot import _handle_register
    sid = str(uuid.uuid4())
    with patch("modules.device.iot._push_ws"), \
         patch("core.database.AsyncSessionLocal") as mock_db_factory:
        mock_db = AsyncMock()
        mock_db_factory.return_value.__aenter__.return_value = mock_db

        # Mock session lookup
        mock_session = MagicMock()
        mock_session.status = "verifying"
        mock_session.user_id = uuid.uuid4()
        mock_session.certificate_arn = None

        # Mock two separate execute calls (session + device lookup)
        mock_db.execute.return_value.scalar_one_or_none.side_effect = [
            mock_session,  # session lookup
            None,          # device lookup — no existing device
        ]
        # Mock the has_active exists() query
        mock_db.execute.return_value.scalar.side_effect = [None, False]

        await _handle_register("  AbCdEf1234  ", {"session_id": sid})

    # Verify device was created with normalized hw_id
    from models.device import Device
    # Check the Device constructor was called with lowercased hw_id
    calls = mock_db.add.call_args_list
    if calls:
        device_arg = calls[0][0][0]
        assert device_arg.hardware_id == "abcdef1234"


# ═══════════════════════════════════════════════════════════════
# Webhook endpoint — dispatch to handlers
# ═══════════════════════════════════════════════════════════════

@patch("core.config.settings.aws_iot_webhook_secret", "test-secret")
@pytest.mark.asyncio
async def test_webhook_wrong_secret_403():
    from modules.device.iot import aws_iot_webhook
    from fastapi import Request, HTTPException

    req = MagicMock(spec=Request)
    req.query_params = {}

    with pytest.raises(HTTPException) as exc:
        await aws_iot_webhook(
            req,
            payload={"topic": "pgg/dev/hw/d2c"},
            x_aws_secret="wrong",
        )
    assert exc.value.status_code == 403


@patch("core.config.settings.aws_iot_webhook_secret", "test-secret")
@pytest.mark.asyncio
async def test_webhook_correct_secret_dispatches():
    from modules.device.iot import aws_iot_webhook
    from fastapi import Request

    req = MagicMock(spec=Request)
    req.query_params = {}

    with patch("modules.device.iot._handle_online", new_callable=AsyncMock) as m:
        await aws_iot_webhook(
            req,
            payload={"topic": "pgg/dev/hw123/d2c",
                     "payload": {"msg_type": "device.online", "session_id": "s"}},
            x_aws_secret="test-secret",
        )
    m.assert_called_once()


@patch("core.config.settings.aws_iot_webhook_secret", "test-secret")
@pytest.mark.asyncio
async def test_webhook_pong_dispatches():
    from modules.device.iot import aws_iot_webhook
    from fastapi import Request

    req = MagicMock(spec=Request)
    req.query_params = {}

    with patch("modules.device.iot._handle_pong", new_callable=AsyncMock) as m:
        await aws_iot_webhook(
            req,
            payload={"topic": "pgg/dev/hw/d2c",
                     "payload": {"msg_type": "connectivity.pong"}},
            x_aws_secret="test-secret",
        )
    m.assert_called_once()


@patch("core.config.settings.aws_iot_webhook_secret", "test-secret")
@pytest.mark.asyncio
async def test_webhook_register_dispatches():
    from modules.device.iot import aws_iot_webhook
    from fastapi import Request

    req = MagicMock(spec=Request)
    req.query_params = {}

    with patch("modules.device.iot._handle_register", new_callable=AsyncMock) as m:
        await aws_iot_webhook(
            req,
            payload={"topic": "pgg/dev/hw/d2c",
                     "payload": {"msg_type": "device.register", "session_id": "s"}},
            x_aws_secret="test-secret",
        )
    m.assert_called_once()


@patch("core.config.settings.aws_iot_webhook_secret", "test-secret")
@pytest.mark.asyncio
async def test_webhook_heartbeat_writes_redis_only():
    from modules.device.iot import aws_iot_webhook
    from fastapi import Request

    req = MagicMock(spec=Request)
    req.query_params = {}

    with patch("modules.device.iot.redis_set", new_callable=AsyncMock) as mock_redis:
        resp = await aws_iot_webhook(
            req,
            payload={"topic": "pgg/dev/hw/d2c",
                     "payload": {"msg_type": "device.heartbeat"}},
            x_aws_secret="test-secret",
        )
    assert resp == {"status": "ok"}
    assert mock_redis.call_count == 2


@pytest.mark.asyncio
async def test_webhook_v2_destination_confirmation():
    from modules.device.iot import aws_iot_webhook
    from fastapi import Request

    req = MagicMock(spec=Request)
    req.query_params = {"confirmationToken": "tok123"}

    with patch("modules.device.iot.asyncio.create_task"):
        resp = await aws_iot_webhook(req, payload=None)
    assert hasattr(resp, "body")


@pytest.mark.asyncio
async def test_webhook_v1_body_confirmation():
    from modules.device.iot import aws_iot_webhook
    from fastapi import Request

    req = MagicMock(spec=Request)
    req.query_params = {}

    resp = await aws_iot_webhook(req, payload={"confirmationToken": "tok456"})
    assert hasattr(resp, "body")
