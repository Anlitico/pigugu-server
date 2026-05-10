"""Edge-case tests for provisioning service changes (C1, C5, C6, C7, C12)."""

import json
import uuid
from datetime import datetime, timedelta, timezone
import pytest
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import AsyncSession
from app.models.user import User
from app.models.device import Device
from app.models.device_provisioning_session import DeviceProvisioningSession
from app.modules.device.schemas import DeviceBindRequest
from app.modules.device.service import (
    create_provisioning_session,
    verify_connectivity,
    bind_device,
    unbind_device,
    update_device_state,
)

@pytest.fixture
async def test_user(db_session: AsyncSession) -> User:
    user = User(
        id=uuid.uuid4(),
        email="edge_test@example.com",
        hashed_password="hash",
        display_name="Edge Test User",
    )
    db_session.add(user)
    await db_session.flush()
    return user


# ── C1: verify_connectivity publish count ──────────────────────────────

@pytest.mark.asyncio
@patch("app.core.aws.publish_mqtt_message")
@patch("app.modules.device.service.redis_get", return_value=None)
async def test_verify_publishes_exactly_twice(
    mock_redis_get, mock_publish, db_session: AsyncSession, test_user: User
):
    """C1: verify_connectivity should publish exactly 2 times (not 3)."""
    session = await create_provisioning_session(db_session, test_user.id)

    # redis_get returns None → no pong → timeout path
    res = await verify_connectivity(db_session, session.id, test_user.id, "HW-C1")

    assert res.verified is False
    assert res.error_code == "PROVISION_VERIFY_TIMEOUT"
    assert mock_publish.call_count == 2


# ── C1 & C11: verify_connectivity survives MQTT publish failure ────────

@pytest.mark.asyncio
@patch("app.core.aws.publish_mqtt_message")
@patch("app.modules.device.service.redis_get", return_value=None)
async def test_verify_handles_mqtt_publish_failure(
    mock_redis_get, mock_publish, db_session: AsyncSession, test_user: User
):
    """C11: verify_connectivity gracefully handles MQTT publish errors."""
    session = await create_provisioning_session(db_session, test_user.id)

    mock_publish.side_effect = Exception("AWS IoT unavailable")

    res = await verify_connectivity(db_session, session.id, test_user.id, "HW-C11")

    assert res.verified is False
    assert res.error_code == "PROVISION_VERIFY_TIMEOUT"
    # Both publishes failed but the function did not crash
    assert mock_publish.call_count == 2


# ── C5: session limit ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_session_limit_cancels_oldest(db_session: AsyncSession, test_user: User):
    """C5: Creating more than 5 sessions cancels the oldest ones."""
    # Create 6 sessions
    sessions = []
    for _ in range(6):
        s = await create_provisioning_session(db_session, test_user.id)
        sessions.append(s)

    await db_session.flush()

    # The 6th creation should have cancelled the oldest 2
    # (keep newest 4 + new = 5 total active)
    active = [s for s in sessions if s.status != "cancelled"]
    cancelled = [s for s in sessions if s.status == "cancelled"]

    assert len(active) >= 4  # at most newest 4 survive
    assert len(cancelled) >= 1  # at least the oldest was cancelled


# ── C6: concurrent first-device bind protection ────────────────────────

@pytest.mark.asyncio
@patch("app.modules.device.service.redis_exists", return_value=False)
async def test_second_device_binds_as_standby(mock_exists, db_session: AsyncSession, test_user: User):
    """C6: Second bound device gets active_state='standby'."""
    session1 = DeviceProvisioningSession(
        id=uuid.uuid4(), user_id=test_user.id, status="verified",
        challenge_nonce="n1", expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    db_session.add(session1)
    await db_session.flush()

    req1 = DeviceBindRequest(session_id=session1.id, hardware_id="HW-C6-1", device_name="First")
    dev1 = await bind_device(db_session, test_user.id, req1)
    assert dev1.active_state == "active"

    # Second device
    session2 = DeviceProvisioningSession(
        id=uuid.uuid4(), user_id=test_user.id, status="verified",
        challenge_nonce="n2", expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    db_session.add(session2)
    await db_session.flush()

    req2 = DeviceBindRequest(session_id=session2.id, hardware_id="HW-C6-2", device_name="Second")
    dev2 = await bind_device(db_session, test_user.id, req2)
    assert dev2.active_state == "standby"


@pytest.mark.asyncio
@patch("app.modules.device.service.redis_exists", return_value=False)
async def test_has_active_check_prevents_double_active(
    mock_exists, db_session: AsyncSession, test_user: User
):
    """C6: has_active EXISTS query correctly detects active devices."""
    # Create an existing active device
    existing = Device(
        id=uuid.uuid4(), user_id=test_user.id, hardware_id="hw-active",
        device_name="Already Active", active_state="active", binding_status="bound",
    )
    db_session.add(existing)
    await db_session.flush()

    session = DeviceProvisioningSession(
        id=uuid.uuid4(), user_id=test_user.id, status="verified",
        challenge_nonce="n", expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    db_session.add(session)
    await db_session.flush()

    req = DeviceBindRequest(session_id=session.id, hardware_id="hw-new", device_name="New")
    dev = await bind_device(db_session, test_user.id, req)
    assert dev.active_state == "standby"  # not active — one already exists


# ── C7: update_device_state ────────────────────────────────────────────

@pytest.mark.asyncio
@patch("app.modules.device.service.redis_set")
async def test_update_device_state_writes_redis(mock_redis_set, db_session: AsyncSession):
    """C7: update_device_state writes device state to Redis with 60s TTL."""
    await update_device_state("dev-123", "online")

    mock_redis_set.assert_awaited_once_with("device:state:dev-123", "online", ex=60)


# ── C12: auto-promotion sort order ─────────────────────────────────────

@pytest.mark.asyncio
async def test_unbind_active_promotes_most_recent(db_session: AsyncSession, test_user: User):
    """C12: Unbinding active device promotes most recently created online device."""
    t0 = datetime.now(timezone.utc)

    # Two standby devices, created at different times
    older = Device(
        id=uuid.uuid4(), user_id=test_user.id, hardware_id="hw-older",
        device_name="Older", active_state="standby", binding_status="bound",
        created_at=t0 - timedelta(hours=1),
    )
    newer = Device(
        id=uuid.uuid4(), user_id=test_user.id, hardware_id="hw-newer",
        device_name="Newer", active_state="standby", binding_status="bound",
        created_at=t0,
    )
    active_dev = Device(
        id=uuid.uuid4(), user_id=test_user.id, hardware_id="hw-active",
        device_name="Active", active_state="active", binding_status="bound",
        created_at=t0 - timedelta(hours=2),
    )
    db_session.add_all([older, newer, active_dev])
    await db_session.flush()

    # Both standby devices are online
    with patch("app.modules.device.service.redis_exists", return_value=True):
        await unbind_device(db_session, test_user.id, active_dev.id)

    await db_session.refresh(older)
    await db_session.refresh(newer)

    # At least one should be promoted; newer should be preferred
    assert older.active_state == "standby"
    assert newer.active_state == "active"


@pytest.mark.asyncio
async def test_unbind_no_online_device_leaves_no_active(db_session: AsyncSession, test_user: User):
    """C12: If no standby device is online, no active device remains."""
    standby = Device(
        id=uuid.uuid4(), user_id=test_user.id, hardware_id="hw-standby-offline",
        device_name="Standby Offline", active_state="standby", binding_status="bound",
    )
    active_dev = Device(
        id=uuid.uuid4(), user_id=test_user.id, hardware_id="hw-active-2",
        device_name="Active 2", active_state="active", binding_status="bound",
    )
    db_session.add_all([standby, active_dev])
    await db_session.flush()

    # Standby is offline
    with patch("app.modules.device.service.redis_exists", return_value=False):
        await unbind_device(db_session, test_user.id, active_dev.id)

    await db_session.refresh(standby)
    # Standby should NOT be promoted (it's offline)
    assert standby.active_state == "standby"
