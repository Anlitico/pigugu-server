"""Integration tests for iot.py handlers that require a database."""
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.device import Device
from models.device_provisioning_session import DeviceProvisioningSession
from models.user import User


@pytest.fixture
async def test_user(db_session: AsyncSession) -> User:
    user = User(
        id=uuid.uuid4(),
        email="iot_int@example.com",
        hashed_password="hash",
        display_name="IOT Int",
    )
    db_session.add(user)
    await db_session.flush()
    return user


# ── _handle_online (with session, DB path) ───────────────────

@pytest.mark.asyncio
@patch("modules.device.iot.publish_mqtt_message", new_callable=AsyncMock)
@patch("modules.device.iot.redis_set", new_callable=AsyncMock)
async def test_handle_online_with_session_publishes_ping(
    mock_redis, mock_publish, db_session: AsyncSession, test_user: User
):
    from modules.device.iot import _handle_online

    session = DeviceProvisioningSession(
        id=uuid.uuid4(),
        user_id=test_user.id,
        status="created",
        challenge_nonce="test-nonce",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    db_session.add(session)
    await db_session.commit()

    with patch("modules.device.iot._push_ws") as mock_push:
        await _handle_online("test-hw-id", {
            "session_id": str(session.id),
            "ts": 1234,
        })

    # WS pushes: online + verifying
    events = [call[0][1]["type"] for call in mock_push.call_args_list]
    assert "online" in events
    assert "verifying" in events
    # Ping was published
    mock_publish.assert_called_once()


@pytest.mark.asyncio
@patch("modules.device.iot.publish_mqtt_message", new_callable=AsyncMock)
@patch("modules.device.iot.redis_set", new_callable=AsyncMock)
async def test_handle_online_expired_session_no_ping(
    mock_redis, mock_publish, db_session: AsyncSession, test_user: User
):
    from modules.device.iot import _handle_online

    session = DeviceProvisioningSession(
        id=uuid.uuid4(),
        user_id=test_user.id,
        status="created",
        challenge_nonce="nonce",
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    db_session.add(session)
    await db_session.commit()

    with patch("modules.device.iot._push_ws") as mock_push:
        await _handle_online("test-hw", {"session_id": str(session.id)})

    # online push still happens, but no verifying and no ping
    events = [call[0][1]["type"] for call in mock_push.call_args_list]
    assert "online" in events
    assert "verifying" not in events
    mock_publish.assert_not_called()


# ── _handle_register (with DB) ──────────────────────────────

@pytest.mark.asyncio
async def test_handle_register_new_device(db_session: AsyncSession, test_user: User):
    from modules.device.iot import _handle_register

    session = DeviceProvisioningSession(
        id=uuid.uuid4(),
        user_id=test_user.id,
        status="verifying",
        challenge_nonce="nonce",
        certificate_arn="arn:aws:iot:cert/abc",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    db_session.add(session)
    await db_session.commit()

    with patch("modules.device.iot._push_ws") as mock_push:
        await _handle_register("test-hw-1234", {"session_id": str(session.id)})

    # Device created
    result = await db_session.execute(
        select(Device).where(Device.hardware_id == "test-hw-1234")
    )
    device = result.scalar_one()
    assert device.binding_status == "bound"
    assert device.active_state == "active"
    assert device.device_name == "Pigugu 1234"
    assert device.thing_name == "test-hw-1234"

    # Session bound
    await db_session.refresh(session)
    assert session.status == "bound"

    # WS bound
    bound = mock_push.call_args[0][1]
    assert bound["type"] == "bound"


@pytest.mark.asyncio
async def test_handle_register_rebind_unbound_device(
    db_session: AsyncSession, test_user: User
):
    from modules.device.iot import _handle_register

    existing = Device(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),  # different owner
        hardware_id="test-hw-5678",
        device_name="Old",
        binding_status="unbound",
        active_state="standby",
    )
    db_session.add(existing)

    session = DeviceProvisioningSession(
        id=uuid.uuid4(),
        user_id=test_user.id,
        status="verifying",
        challenge_nonce="n",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    db_session.add(session)
    await db_session.commit()

    with patch("modules.device.iot._push_ws") as mock_push:
        await _handle_register("TEST-HW-5678", {"session_id": str(session.id)})

    await db_session.refresh(existing)
    assert existing.binding_status == "bound"
    assert existing.user_id == test_user.id


@pytest.mark.asyncio
async def test_handle_register_already_bound_other_user(
    db_session: AsyncSession, test_user: User
):
    from modules.device.iot import _handle_register

    other = User(id=uuid.uuid4(), email="other@t.com", hashed_password="h")
    db_session.add(other)

    existing = Device(
        id=uuid.uuid4(),
        user_id=other.id,
        hardware_id="test-hw-9999",
        device_name="Taken",
        binding_status="bound",
        active_state="active",
    )
    db_session.add(existing)

    session = DeviceProvisioningSession(
        id=uuid.uuid4(),
        user_id=test_user.id,
        status="verifying",
        challenge_nonce="n",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    db_session.add(session)
    await db_session.commit()

    with patch("modules.device.iot._push_ws") as mock_push:
        await _handle_register("TEST-HW-9999", {"session_id": str(session.id)})

    err = mock_push.call_args[0][1]
    assert err["type"] == "error"
    assert err["error_code"] == "DEVICE_ALREADY_BOUND"


@pytest.mark.asyncio
async def test_handle_register_second_device_is_standby(
    db_session: AsyncSession, test_user: User
):
    from modules.device.iot import _handle_register

    # Existing active device for this user
    existing = Device(
        id=uuid.uuid4(),
        user_id=test_user.id,
        hardware_id="dev-1",
        device_name="First",
        binding_status="bound",
        active_state="active",
    )
    db_session.add(existing)

    session = DeviceProvisioningSession(
        id=uuid.uuid4(),
        user_id=test_user.id,
        status="verifying",
        challenge_nonce="n",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    db_session.add(session)
    await db_session.commit()

    with patch("modules.device.iot._push_ws"):
        await _handle_register("test-hw-second", {"session_id": str(session.id)})

    # Second device should be standby (not active)
    result = await db_session.execute(
        select(Device).where(Device.hardware_id == "test-hw-second")
    )
    device = result.scalar_one()
    assert device.active_state == "standby"
